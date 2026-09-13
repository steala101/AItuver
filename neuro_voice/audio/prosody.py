"""声のトーン解析 (パラ言語)。

発話音声からピッチ (声の高さ)・エネルギー (声の強さ)・話速を推定し、
「その人の普段の声」(セッション内で自動学習するベースライン) との差から
感情的なトーンを日本語ラベルにする。追加モデル不要・numpy のみ・数十ms。

例: 明るく弾んだ声 / テンション高め・興奮気味 / 沈んだ小さな声 /
    強い口調 / ゆっくり落ち着いた声 / 早口
"""
from __future__ import annotations

import logging
import math

import numpy as np

logger = logging.getLogger(__name__)

_F0_MIN = 60.0    # 探索する基本周波数の下限 (Hz)
_F0_MAX = 400.0   # 上限 (Hz)


class ProsodyAnalyzer:
    """発話ごとの声のトーンを推定する。ベースラインは発話を重ねると精緻化する。"""

    def __init__(self, sample_rate: int = 16000, min_samples: int = 3):
        self._sr = sample_rate
        self._min_samples = min_samples  # ラベルを出し始めるまでの発話数
        self._pitch_hist: list[float] = []
        self._energy_hist: list[float] = []
        self._rate_hist: list[float] = []

    # ---------- 特徴量 ----------

    def _frame_rms(self, audio: np.ndarray, frame: int = 512, hop: int = 256) -> np.ndarray:
        if len(audio) < frame:
            return np.array([float(np.sqrt(np.mean(audio ** 2) + 1e-12))])
        n = 1 + (len(audio) - frame) // hop
        idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
        return np.sqrt(np.mean(audio[idx] ** 2, axis=1) + 1e-12)

    def _estimate_pitch(self, audio: np.ndarray) -> float | None:
        """自己相関による中央値F0 (Hz)。有声フレームが少なければ None。"""
        frame, hop = 1024, 512
        if len(audio) < frame * 2:
            return None
        rms = self._frame_rms(audio, frame, hop)
        thr = max(float(np.median(rms)) * 0.8, float(rms.max()) * 0.15)
        lag_min = int(self._sr / _F0_MAX)
        lag_max = int(self._sr / _F0_MIN)
        f0s: list[float] = []
        n = 1 + (len(audio) - frame) // hop
        for i in range(n):
            if rms[i] < thr:
                continue
            x = audio[i * hop : i * hop + frame].astype(np.float64)
            x = x - x.mean()
            ac = np.correlate(x, x, "full")[frame - 1 :]
            if ac[0] <= 0:
                continue
            ac = ac / ac[0]
            seg = ac[lag_min:lag_max]
            if len(seg) == 0:
                continue
            peak = int(np.argmax(seg)) + lag_min
            if ac[peak] < 0.45:  # 周期性が弱い (無声音など)
                continue
            f0s.append(self._sr / peak)
        if len(f0s) < 3:
            return None
        return float(np.median(f0s))

    # ---------- 解析 ----------

    def analyze(self, audio: np.ndarray, text: str = "") -> dict | None:
        """発話音声を解析してトーン情報を返す。判定不能なら None。

        返り値: {"label": str, "pitch_hz", "pitch_st", "energy_db", "rate"}
          pitch_st  … ベースラインとの差 (半音)
          energy_db … ベースラインとの差 (dB)
          rate      … 話速のベースライン比
        """
        try:
            audio = np.asarray(audio, dtype=np.float32)
            dur = len(audio) / self._sr
            if dur < 0.4:
                return None

            pitch = self._estimate_pitch(audio)
            rms = self._frame_rms(audio)
            # 上位50%の平均 = 発話部分のエネルギー
            energy = float(np.mean(np.sort(rms)[len(rms) // 2 :]))
            rate = (len(text.strip()) / dur) if text.strip() else None

            ready = len(self._energy_hist) >= self._min_samples
            label = None
            pitch_st = 0.0
            energy_db = 0.0
            rate_ratio = 1.0
            if ready:
                if pitch and self._pitch_hist:
                    base_p = float(np.median(self._pitch_hist))
                    pitch_st = 12.0 * math.log2(pitch / base_p)
                base_e = float(np.median(self._energy_hist))
                energy_db = 20.0 * math.log10(energy / max(base_e, 1e-9))
                if rate and self._rate_hist:
                    base_r = float(np.median(self._rate_hist))
                    rate_ratio = rate / max(base_r, 1e-6)
                label = self._label(pitch_st, energy_db, rate_ratio, pitch is not None)

            # ベースライン更新 (直近20発話の中央値)
            if pitch:
                self._pitch_hist.append(pitch)
                self._pitch_hist = self._pitch_hist[-20:]
            self._energy_hist.append(energy)
            self._energy_hist = self._energy_hist[-20:]
            if rate:
                self._rate_hist.append(rate)
                self._rate_hist = self._rate_hist[-20:]

            if label is None:
                return None
            return {
                "label": label,
                "pitch_hz": round(pitch, 1) if pitch else None,
                "pitch_st": round(pitch_st, 2),
                "energy_db": round(energy_db, 2),
                "rate": round(rate_ratio, 2),
            }
        except Exception:
            logger.exception("声のトーン解析でエラー")
            return None

    @staticmethod
    def _label(st: float, db: float, rate: float, has_pitch: bool) -> str:
        """ベースラインとの差から日本語のトーンラベルを作る。"""
        parts: list[str] = []
        if has_pitch and st >= 2.0 and db >= 2.0:
            parts.append("テンション高め・興奮気味の声")
        elif has_pitch and st >= 1.5:
            parts.append("明るく弾んだ声")
        elif db >= 3.5 and (not has_pitch or st <= 0.5):
            parts.append("強い口調")
        elif has_pitch and st <= -1.5 and db <= -2.0:
            parts.append("沈んだ小さな声")
        elif db <= -3.5:
            parts.append("小さな声")
        elif has_pitch and st <= -1.5:
            parts.append("低めの落ち着いた声")

        if rate >= 1.3:
            parts.append("早口")
        elif rate <= 0.72:
            parts.append("ゆっくりめ")

        if not parts:
            return "いつも通りの落ち着いた声"
        return "、".join(parts)
