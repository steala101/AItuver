"""音声会話中の相槌・割り込みを判定する軽量なルール層。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


class SpeechIntent(StrEnum):
    """AI 発話中に検出したユーザー音声の扱い。"""

    ACKNOWLEDGEMENT = "acknowledgement"
    INTERRUPTION = "interruption"
    FALSE_POSITIVE = "false_positive"


@dataclass(frozen=True)
class SpeechClassification:
    intent: SpeechIntent
    normalized_text: str
    reason: str


class InteractionClassifier:
    """誤停止を避けるための、保守的な一次判定。

    音声認識結果が短い相槌に完全一致したときだけ相槌として扱う。曖昧な
    発話は取りこぼしより会話性を優先して割り込みに分類する。
    """

    _ACKS = frozenset({
        "うん", "うんうん", "はい", "はーい", "ええ", "そう", "そうそう",
        "そうだね", "そうですね", "なるほど", "へえ", "ふーん", "ふむ",
        "たしかに", "確かに", "わかった", "分かった", "おっけー", "ok", "okay",
        "いいね", "いいですね", "なるほどね", "あー", "ああ", "んー",
    })
    _NOISE = frozenset({"え", "あ", "う", "ん", "えー", "あー", "んー", "...", "…"})
    # 相づちの後ろに付く語尾 (「うんうんね」等)。単独では相づちにならない
    _TAILS = frozenset({"ね", "よ", "な", "ー", "っ", "わ"})
    # 笑い声 (STTの定番認識)。割り込みでも相づちでもなく、再生を続ける
    _LAUGH_WORDS = frozenset({"笑", "(笑)", "w", "ww", "www", "草"})
    _LAUGH_RE = re.compile(r"^[あわえおうん]?[はひふへほゃ]{2,}[はひふへほあー]*$")

    def __init__(self, acknowledgement_max_chars: int = 12):
        self._acknowledgement_max_chars = max(1, acknowledgement_max_chars)
        # 複合相づち分解用トークン (長い順に照合)
        self._tokens = sorted(
            self._ACKS | self._NOISE | self._TAILS, key=len, reverse=True,
        )

    def classify(self, text: str, duration_ms: float | None = None) -> SpeechClassification:
        normalized = self.normalize(text)
        if not normalized:
            return SpeechClassification(SpeechIntent.FALSE_POSITIVE, normalized, "empty_transcript")
        if normalized in self._NOISE and (duration_ms is None or duration_ms < 900):
            return SpeechClassification(SpeechIntent.FALSE_POSITIVE, normalized, "brief_filler")
        # 笑い声は割り込みとして扱わない (咳・笑いで停止しない: 指示書フェーズ4)
        if normalized in self._LAUGH_WORDS or self._LAUGH_RE.fullmatch(normalized):
            return SpeechClassification(SpeechIntent.FALSE_POSITIVE, normalized, "laughter")
        if len(normalized) <= self._acknowledgement_max_chars and normalized in self._ACKS:
            return SpeechClassification(SpeechIntent.ACKNOWLEDGEMENT, normalized, "known_short_ack")
        # 複合相づち: 「うんうんそうそう」「はいはいなるほどね」のように、
        # 既知の相づち+フィラー+語尾だけで構成される発話は相づちとして扱う。
        # 「うん、でもさ」のように相づち以外の語が続く場合は割り込みのまま。
        if len(normalized) <= 14 and self._is_ack_composite(normalized):
            return SpeechClassification(SpeechIntent.ACKNOWLEDGEMENT, normalized, "composite_ack")
        return SpeechClassification(SpeechIntent.INTERRUPTION, normalized, "substantive_utterance")

    def _is_ack_composite(self, normalized: str) -> bool:
        """相づち・フィラー・語尾トークンだけで全体を構成できるか (要: 相づち1つ以上)。"""
        # normalize()は端の句読点しか落とさないため、中間の読点も除いてから分解する
        normalized = re.sub(r"[、。,.]", "", normalized)
        i = 0
        found_ack = False
        while i < len(normalized):
            for token in self._tokens:
                if normalized.startswith(token, i):
                    if token in self._ACKS:
                        found_ack = True
                    i += len(token)
                    break
            else:
                return False  # 分解できない語が残った → 実質発話
        return found_ack

    @staticmethod
    def normalize(text: str) -> str:
        text = text.strip().lower()
        text = re.sub(r"[\s\u3000]+", "", text)
        return text.strip("、。！？!?・….,，")


class ResponseAcknowledgementPlanner:
    """ユーザー発話が終わった後、応答の先頭に置く短い相槌を決める。"""

    def __init__(self, probability: float = 0.38):
        self._probability = max(0.0, min(1.0, probability))
        self._last: str | None = None

    def choose(self, text: str, emotion: str | None, random_value: float, random_index: int) -> str | None:
        # 短い質問・命令への毎回の相槌は不自然なので、主に話を聞いた時だけ付ける。
        normalized = InteractionClassifier.normalize(text)
        if len(normalized) < 10 or text.strip().endswith(("?", "？")):
            return None
        if random_value >= self._probability:
            return None
        if emotion == "sad":
            pool = ("うーん", "そっか", "うんうん")
        elif emotion in {"joy", "fun"} or any(word in normalized for word in ("笑", "面白", "草")):
            pool = ("あはは", "うんうん", "いいね")
        elif emotion == "angry":
            pool = ("うーん", "うんうん")
        else:
            pool = ("うんうん", "うーん", "そっか")
        candidates = [phrase for phrase in pool if phrase != self._last] or list(pool)
        chosen = candidates[random_index % len(candidates)]
        self._last = chosen
        return chosen
