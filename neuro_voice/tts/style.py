"""LLMの話し方指示を、各TTSが使う具体的な音声パラメータへ変換する層。"""
from __future__ import annotations

from dataclasses import dataclass

from neuro_voice.utils.emotion import voice_params_for


DELIVERIES = frozenset({
    "neutral", "calm", "gentle", "lively", "playful", "serious", "warm", "soft",
})

# すべて基準値への倍率。pitchだけはVOICEVOXのpitchScaleへの加算値。
_DELIVERY_PARAMS: dict[str, dict[str, float]] = {
    "neutral": {"speed": 1.00, "pitch": 0.00, "intonation": 1.00, "volume": 1.00, "pre": 0.05, "post": 0.10},
    "calm":    {"speed": 0.90, "pitch": -0.01, "intonation": 0.82, "volume": 0.90, "pre": 0.07, "post": 0.16},
    "gentle":  {"speed": 0.94, "pitch": 0.01, "intonation": 0.90, "volume": 0.92, "pre": 0.06, "post": 0.13},
    "lively":  {"speed": 1.08, "pitch": 0.02, "intonation": 1.18, "volume": 1.06, "pre": 0.04, "post": 0.08},
    "playful": {"speed": 1.04, "pitch": 0.03, "intonation": 1.24, "volume": 1.00, "pre": 0.04, "post": 0.08},
    "serious": {"speed": 0.95, "pitch": -0.02, "intonation": 0.92, "volume": 1.00, "pre": 0.06, "post": 0.12},
    "warm":    {"speed": 0.97, "pitch": 0.01, "intonation": 1.04, "volume": 0.98, "pre": 0.05, "post": 0.12},
    "soft":    {"speed": 0.92, "pitch": 0.00, "intonation": 0.84, "volume": 0.82, "pre": 0.07, "post": 0.15},
}

# ユーザー側の感情が高確信で取れた場合だけ、AIの声へごく小さく反映する。
_USER_EMOTION_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "neutral":   {"speed": 1.00, "intonation": 1.00, "volume": 1.00},
    "joy":       {"speed": 1.02, "intonation": 1.03, "volume": 1.01},
    "fun":       {"speed": 1.02, "intonation": 1.04, "volume": 1.01},
    "angry":     {"speed": 0.97, "intonation": 0.96, "volume": 0.97},
    "sad":       {"speed": 0.96, "intonation": 0.94, "volume": 0.96},
    "surprised": {"speed": 1.01, "intonation": 1.03, "volume": 1.00},
}


@dataclass(frozen=True)
class SpeechStyle:
    emotion: str = "neutral"
    delivery: str = "neutral"
    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    volume: float = 1.0
    pre_phoneme: float = 0.05
    post_phoneme: float = 0.10


class StyleManager:
    """感情と話し方ラベルから、細かな数値を一元的に決める。"""

    def __init__(self, user_emotion_threshold: float = 0.75):
        self._user_emotion_threshold = max(0.0, min(1.0, user_emotion_threshold))

    def resolve(
        self,
        emotion: str | None,
        delivery: str | None,
        *,
        user_emotion: str | None = None,
        user_confidence: float = 0.0,
    ) -> SpeechStyle:
        emo = (emotion or "neutral").lower()
        mode = (delivery or "neutral").lower()
        if mode not in DELIVERIES:
            mode = "neutral"
        e = voice_params_for(emo)
        d = _DELIVERY_PARAMS[mode]
        user = _USER_EMOTION_ADJUSTMENTS.get((user_emotion or "neutral").lower(), _USER_EMOTION_ADJUSTMENTS["neutral"])
        # 閾値を超えた分だけ最大35%で混ぜるため、ユーザー感情がAIの人格を上書きしない。
        influence = 0.0
        if user_confidence >= self._user_emotion_threshold:
            influence = min(0.35, (user_confidence - self._user_emotion_threshold) / max(1e-6, 1 - self._user_emotion_threshold) * 0.35)

        def blend(key: str) -> float:
            return 1.0 + (user[key] - 1.0) * influence

        return SpeechStyle(
            emotion=emo,
            delivery=mode,
            speed=e["speed"] * d["speed"] * blend("speed"),
            pitch=e["pitch"] + d["pitch"],
            intonation=e["intonation"] * d["intonation"] * blend("intonation"),
            volume=e["volume"] * d["volume"] * blend("volume"),
            pre_phoneme=d["pre"],
            post_phoneme=d["post"],
        )
