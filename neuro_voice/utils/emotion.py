"""ストリーム冒頭の感情・話し方タグを検出・除去する。

LLM には System Prompt で応答冒頭に感情タグを出力させ、
表示・TTS 前にここで取り除く。将来はアバターの表情制御にも使う。
"""
from __future__ import annotations

import re
from typing import Callable

EMOTIONS = {"neutral", "joy", "fun", "angry", "sad", "surprised"}
DELIVERIES = {"neutral", "calm", "gentle", "lively", "playful", "serious", "warm", "soft"}
# Some local models occasionally emit a bracketed marker for their intended
# response mode. These markers are metadata and must not reach the UI, chat
# history, or TTS. Keep the list explicit so ordinary text such as ``[Python]``
# remains untouched.
NON_SPOKEN_DIRECTIVES = {
    "thought", "thinking", "analysis", "reasoning", "internal", "observation",
}

# 感情 → 声の調整。speed/intonation/volume は基準値への倍率、pitch は加算オフセット
# (VOICEVOXのpitchScaleは0基準・±0.15程度)。喋りの抑揚・大きさ・速さを自動で変える。
EMOTION_VOICE: dict[str, dict[str, float]] = {
    "neutral":   {"speed": 1.00, "pitch": 0.00, "intonation": 1.00, "volume": 1.00},
    "joy":       {"speed": 1.08, "pitch": 0.03, "intonation": 1.30, "volume": 1.12},
    "fun":       {"speed": 1.12, "pitch": 0.02, "intonation": 1.25, "volume": 1.08},
    "angry":     {"speed": 1.06, "pitch": -0.01, "intonation": 1.45, "volume": 1.25},
    "sad":       {"speed": 0.86, "pitch": -0.04, "intonation": 0.70, "volume": 0.88},
    "surprised": {"speed": 1.10, "pitch": 0.05, "intonation": 1.45, "volume": 1.15},
}


def voice_params_for(emotion: str | None) -> dict[str, float]:
    """感情に対応する声の調整係数を返す (未知/Noneは neutral)。"""
    return EMOTION_VOICE.get((emotion or "neutral").lower(), EMOTION_VOICE["neutral"])

# [joy] / [emotion: joy] / [style: calm] の形式を受け付ける。
_TAG_RE = re.compile(
    r"^\s*[\[【]\s*(?:(emotion|style)\s*[:：]\s*)?([A-Za-z_]+)\s*[\]】]\s*",
    re.IGNORECASE,
)
_TYPED_TAG_RE = re.compile(
    r"[\[【]\s*(emotion|style)\s*[:：]\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[\]】]",
    re.IGNORECASE,
)
_INLINE_TAG_RE = re.compile(
    r"[\[【]\s*(?:(emotion|style)\s*[:：]\s*)?"
    r"(neutral|joy|fun|angry|sad|surprised|calm|gentle|lively|playful|serious|warm|soft)\s*[\]】]",
    re.IGNORECASE,
)
_NON_SPOKEN_NAMES = "|".join(
    re.escape(name) for name in sorted(NON_SPOKEN_DIRECTIVES, key=len, reverse=True)
)
_NON_SPOKEN_TAG_RE = re.compile(
    rf"(?:\[|【)\s*/?\s*({_NON_SPOKEN_NAMES})\s*(?:\]|】)",
    re.IGNORECASE,
)

# An LLM stream may split ``[fun]`` into three tokens: ``[`` / ``fun`` /
# ``]``.  Holding only *initial* tags allowed the latter two tokens to leak to
# the UI and TTS once ordinary text had already been emitted.  Keep a possible
# trailing tag fragment across every token boundary.
_TAG_FRAGMENT_RE = re.compile(
    r"[\[【]\s*(?:(?:emotion|style)\s*[:：]\s*)?[A-Za-z_]*\s*$",
    re.IGNORECASE,
)

_MAX_BUFFER = 24  # ここまで溜めてタグが見つからなければ諦めて流す


class EmotionTagParser:
    """先頭の感情・話し方タグを取り除き、各コールバックへ通知する。

    クラス名は既存コードとの互換性のため維持している。複数の先頭タグを
    受け取れるため、LLMは ``[emotion: joy][style: lively]`` と出力できる。
    """

    def __init__(
        self,
        on_emotion: Callable[[str], None] | None = None,
        on_style: Callable[[str], None] | None = None,
    ):
        self._on_emotion = on_emotion
        self._on_style = on_style
        self._buf = ""
        self._decided = False
        self.emotion: str | None = None
        self.style: str | None = None

    def feed(self, token: str) -> str:
        """トークンを通し、表示・TTSに渡してよいテキストを返す。"""
        self._buf += token
        out: list[str] = []
        cursor = 0
        matches = [
            *((match.start(), match, "typed") for match in _TYPED_TAG_RE.finditer(self._buf)),
            *((match.start(), match, "known") for match in _INLINE_TAG_RE.finditer(self._buf)),
            *((match.start(), match, "directive") for match in _NON_SPOKEN_TAG_RE.finditer(self._buf)),
        ]
        for _start, match, tag_kind in sorted(matches, key=lambda item: item[0]):
            if match.start() < cursor:
                continue
            out.append(self._buf[cursor:match.start()])
            if tag_kind in {"typed", "known"}:
                self._apply_tag(match.group(1) or "", match.group(2))
            cursor = match.end()
        remainder = self._buf[cursor:]

        # Preserve only a possible *trailing* tag fragment.  Everything before
        # it can be displayed and segmented for TTS immediately.
        fragment = _TAG_FRAGMENT_RE.search(remainder)
        if fragment is not None and len(remainder) - fragment.start() <= _MAX_BUFFER:
            out.append(remainder[:fragment.start()])
            self._buf = remainder[fragment.start():]
        else:
            out.append(remainder)
            self._buf = ""
        self._decided = not bool(self._buf)
        return "".join(out)

    def _apply_tag(self, kind: str, tag: str) -> None:
        kind, tag = kind.lower(), tag.lower()
        if kind == "style" or (not kind and tag in DELIVERIES and tag not in EMOTIONS):
            if tag in DELIVERIES:
                self.style = tag
                if self._on_style is not None:
                    self._on_style(tag)
        elif tag in EMOTIONS:
            self.emotion = tag
            if self._on_emotion is not None:
                self._on_emotion(tag)

    def flush(self) -> str:
        """ストリーム終了時、保留中のテキストを返す。"""
        out = self._buf
        self._buf = ""
        self._decided = True
        # A truncated internal tag is metadata, not spoken content.
        if _TAG_FRAGMENT_RE.fullmatch(out):
            return ""
        return _NON_SPOKEN_TAG_RE.sub(
            "", _INLINE_TAG_RE.sub("", _TYPED_TAG_RE.sub("", out)),
        )
