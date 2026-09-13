"""openSMILE特徴量とSpeechBrain分類器を使うユーザー音声感情推定。"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmotionResult:
    label: str
    confidence: float
    probabilities: dict[str, float]
    feature_count: int


class OpenSmileSpeechBrainRecognizer:
    """音響特徴と確率分類を分離して扱う、遅延ロード型の感情認識器。

    openSMILE は eGeMAPS 特徴量を抽出して入力品質・将来の再学習用に保持し、
    SpeechBrain の事前学習済み分類器が波形から感情確率を返す。両者を無理に
    同じテンソルへ結合せず、モデルの入力仕様を壊さない構成にしている。
    """

    _LABEL_MAP = {
        "neu": "neutral", "neutral": "neutral", "calm": "neutral",
        "hap": "joy", "happy": "joy", "happiness": "joy", "joy": "joy",
        "exc": "fun", "excited": "fun", "surprise": "surprised", "surprised": "surprised",
        "ang": "angry", "angry": "angry", "fru": "angry", "frustrated": "angry",
        "sad": "sad", "sadness": "sad",
    }

    def __init__(
        self,
        *,
        feature_set: str = "eGeMAPSv02",
        model_source: str = "speechbrain/emotion-recognition-wav2vec2-IEMOCAP",
        savedir: str = "models/speechbrain_emotion",
        device: str = "cpu",
    ):
        self._feature_set = feature_set
        self._model_source = model_source
        self._savedir = str(Path(savedir))
        self._device = device
        self._smile = None
        self._classifier = None
        self._torch = None
        self._load_failed = False

    def analyze(self, audio: np.ndarray, sample_rate: int) -> EmotionResult | None:
        if not self._ensure_loaded():
            return None
        try:
            wave = np.asarray(audio, dtype=np.float32).reshape(-1)
            if len(wave) < sample_rate // 2:
                return None
            features = self._smile.process_signal(wave, sampling_rate=sample_rate)
            wav_tensor = self._torch.from_numpy(wave).unsqueeze(0)
            with self._torch.no_grad():
                logits, _, _, labels = self._classifier.classify_batch(wav_tensor)
            probs = self._torch.softmax(logits.squeeze(0), dim=-1).detach().cpu().numpy()
            raw_labels = self._classifier_labels(labels, len(probs))
            mapped: dict[str, float] = {}
            for raw, probability in zip(raw_labels, probs, strict=False):
                label = self._LABEL_MAP.get(str(raw).lower(), "neutral")
                mapped[label] = mapped.get(label, 0.0) + float(probability)
            if not mapped:
                return None
            label, confidence = max(mapped.items(), key=lambda item: item[1])
            return EmotionResult(
                label=label,
                confidence=round(confidence, 4),
                probabilities={key: round(value, 4) for key, value in mapped.items()},
                feature_count=int(features.shape[1]),
            )
        except Exception:
            logger.exception("SpeechBrain感情推定に失敗")
            return None

    def _ensure_loaded(self) -> bool:
        if self._load_failed:
            return False
        if self._classifier is not None:
            return True
        try:
            import opensmile
            import torch
            from speechbrain.inference.classifiers import EncoderClassifier

            self._smile = opensmile.Smile(
                feature_set=getattr(opensmile.FeatureSet, self._feature_set),
                feature_level=opensmile.FeatureLevel.Functionals,
            )
            self._classifier = EncoderClassifier.from_hparams(
                source=self._model_source,
                savedir=self._savedir,
                run_opts={"device": self._device},
            )
            self._torch = torch
            logger.info("音声感情認識をロード: openSMILE=%s / SpeechBrain=%s", self._feature_set, self._model_source)
            return True
        except Exception as exc:
            self._load_failed = True
            logger.warning("音声感情認識を無効化します (openSMILE/SpeechBrain未導入またはモデル取得失敗): %s", exc)
            return False

    def _classifier_labels(self, labels, size: int) -> list[str]:
        """SpeechBrainの版差を吸収して、各確率のクラス名を取り出す。"""
        encoder = getattr(getattr(self._classifier, "hparams", None), "label_encoder", None)
        ind2lab = getattr(encoder, "ind2lab", {}) if encoder is not None else {}
        if ind2lab:
            return [str(ind2lab.get(i, i)) for i in range(size)]
        if labels is not None:
            try:
                first = labels[0]
                if isinstance(first, (list, tuple)) and len(first) == size:
                    return [str(item) for item in first]
            except (IndexError, TypeError):
                pass
        return [str(i) for i in range(size)]
