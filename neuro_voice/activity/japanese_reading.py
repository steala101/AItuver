"""Japanese surface-to-reading normalization shared by stateful activities.

An activity validator must compare readings, not whatever spelling happened to
come out of STT.  The service therefore has one canonical normalization path for
human and assistant moves.  Optional Japanese NLP packages are used when they
are already installed; the application remains usable without them.
"""
from __future__ import annotations

from functools import lru_cache
import logging
import re
from typing import Mapping

logger = logging.getLogger(__name__)

_KANJI_RE = re.compile(r"[一-龥々〆ヶ]")
_KANA_RE = re.compile(r"^[ぁ-ゖァ-ヺー]+$")
_EDGE_RE = re.compile(
    r"^(?:じゃあ|それじゃ|次は|はい|うん)[、,\s]*|"
    r"(?:だよ|です|かな)(?:[。!?！？、,\s]*)$|[。!?！？、,\s]+$"
)

# This fallback is intentionally broader than the old per-module dictionaries,
# but it is not the primary architecture.  A user dictionary or an installed
# morphological backend can resolve words outside this seed.
DEFAULT_READINGS: dict[str, str] = {
    "林檎": "りんご", "蜜柑": "みかん", "達磨": "だるま", "苺": "いちご",
    "兎": "うさぎ", "狐": "きつね", "車": "くるま", "桜": "さくら",
    "西瓜": "すいか", "空": "そら", "象": "ぞう", "猫": "ねこ",
    "蛇": "へび", "葡萄": "ぶどう", "米": "こめ", "お米": "こめ",
    "御飯": "ごはん", "ご飯": "ごはん", "飯": "めし", "水": "みず",
    "山": "やま", "川": "かわ", "海": "うみ", "雨": "あめ",
    "雪": "ゆき", "花": "はな", "鳥": "とり", "犬": "いぬ",
    "魚": "さかな", "虫": "むし", "星": "ほし", "月": "つき",
    "太陽": "たいよう", "時計": "とけい", "眼鏡": "めがね",
    "電話": "でんわ", "机": "つくえ", "椅子": "いす", "鉛筆": "えんぴつ",
}

_SMALL = str.maketrans("ぁぃぅぇぉゃゅょゎっ", "あいうえおやゆよわつ")


def katakana_to_hiragana(text: str) -> str:
    return "".join(
        chr(ord(char) - 0x60) if "ァ" <= char <= "ヶ" else char
        for char in str(text or "")
    )


def normalize_kana(text: str) -> str:
    value = katakana_to_hiragana(text).translate(_SMALL)
    return re.sub(r"[^ぁ-ゖー]", "", value)


_LONG_VOWEL = {
    **{char: "あ" for char in "かがさざただなはばぱまやらわ"},
    **{char: "い" for char in "きぎしじちぢにひびぴみり"},
    **{char: "う" for char in "くぐすずつづぬふぶぷむゆる"},
    **{char: "え" for char in "けげせぜてでねへべぺめれ"},
    **{char: "お" for char in "こごそぞとどのほぼぽもよろを"},
}


def first_kana(word: str) -> str:
    reading = normalize_kana(word)
    return reading[:1]


def last_kana(word: str) -> str:
    reading = normalize_kana(word)
    if not reading:
        return ""
    if reading[-1] == "ー" and len(reading) >= 2:
        return _LONG_VOWEL.get(reading[-2], reading[-2])
    return reading[-1]


class JapaneseReadingService:
    """Resolve a short Japanese word to one stable hiragana comparison key."""

    def __init__(self, user_readings: Mapping[str, str] | None = None):
        self._readings = dict(DEFAULT_READINGS)
        for surface, reading in dict(user_readings or {}).items():
            normalized = normalize_kana(str(reading))
            if str(surface).strip() and normalized:
                self._readings[str(surface).strip()] = normalized
        self._backend = self._load_backend()

    @staticmethod
    def _load_backend():
        try:
            import pyopenjtalk  # type: ignore

            return ("pyopenjtalk", pyopenjtalk)
        except Exception:
            pass
        try:
            from pykakasi import kakasi  # type: ignore

            converter = kakasi()
            return ("pykakasi", converter)
        except Exception:
            return None

    @staticmethod
    def clean_surface(text: str) -> str:
        value = str(text or "").strip("「『」』\"' \t\r\n")
        previous = None
        while previous != value:
            previous = value
            value = _EDGE_RE.sub("", value).strip("「『」』\"' \t\r\n")
        return value

    @lru_cache(maxsize=2048)
    def reading(self, surface: str) -> str:
        clean = self.clean_surface(surface)
        if not clean:
            return ""
        if clean in self._readings:
            return self._readings[clean]
        if _KANA_RE.fullmatch(clean):
            return normalize_kana(clean)
        if not _KANJI_RE.search(clean):
            return ""
        if self._backend is None:
            return ""
        name, backend = self._backend
        try:
            if name == "pyopenjtalk":
                value = backend.g2p(clean, kana=True)
            else:
                value = "".join(
                    str(item.get("hira") or item.get("kana") or item.get("orig") or "")
                    for item in backend.convert(clean)
                )
            normalized = normalize_kana(value)
            # A short activity word cannot legitimately normalize to nothing or
            # retain unknown non-kana symbols.
            return normalized if normalized else ""
        except Exception:
            logger.debug("Japanese reading backend failed for %r", clean, exc_info=True)
            return ""

    def extract_word(self, text: str) -> tuple[str, str]:
        """Return ``(spoken surface, hiragana key)`` for a short one-word move."""
        clean = self.clean_surface(text)
        if not clean:
            return "", ""
        direct = self.reading(clean)
        if direct:
            return clean, direct

        # Prefer a quoted candidate in otherwise conversational text.
        quoted = re.findall(r"[「『\"]([^」』\"]{1,24})[」』\"]", str(text or ""))
        for candidate in quoted:
            reading = self.reading(candidate)
            if reading:
                return candidate, reading

        # A kana run is safe as a final fallback; kanji fragments are never
        # guessed because a wrong reading would corrupt the canonical ledger.
        matches = re.findall(r"[ぁ-ゖァ-ヺー]{2,24}", clean)
        if matches:
            candidate = matches[-1]
            return candidate, normalize_kana(candidate)
        return clean, ""
