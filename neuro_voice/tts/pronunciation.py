"""Surface-form to kana pronunciation overrides for Japanese TTS."""
from __future__ import annotations

import re


_KANA_CHAR = r"[ぁ-んァ-ヶー]"
_KANA = rf"{_KANA_CHAR}+"
_SURFACE = r"[一-龥々〆ヶ][一-龥々〆ヶぁ-んァ-ヶー]{0,15}"
_NOT = r"(?:じゃなくて|ではなくて|ではなく)"
# 「と/って」の連結を必須にする。これが任意だと「今名前をなんで覚えてる?」の
# ような普通の疑問文が (今名前→なんで) として誤学習される。
# また「覚えてる/覚えている」(状態の質問) は学習トリガーにしない。
_EXPLICIT = re.compile(
    rf"[「『]?({_SURFACE})[」』]?\s*(?:の読み(?:方)?は|は|を)\s*"
    rf"[「『]?({_KANA_CHAR}{{2,32}})[」』]?\s*(?:と|って)\s*"
    rf"(?:読む|よむ|読みます|発音(?:する|します)?|覚えて(?!る|いる|います|た|ない))"
)
# surface は怠惰マッチにする。貪欲だと「深掘りの読み方は…」の「の」まで
# 吸収して「深掘りの」を表記として誤登録する。
_READING_LABEL = re.compile(
    rf"[「『]?([一-龥々〆ヶ][一-龥々〆ヶぁ-んァ-ヶー]{{0,15}}?)[」』]?\s*の?読み(?:方)?\s*(?:は|=|：|:)\s*"
    rf"[「『]?({_KANA_CHAR}{{2,32}})[」』]?(?=$|[。！？!?、,\s])"
)
_READING_ONLY = re.compile(
    rf"(?P<wrong>{_KANA_CHAR}{{2,}})\s*{_NOT}[、,\s]*"
    rf"(?P<reading>{_KANA_CHAR}{{2,}}?)(?=(?:って|と)?\s*(?:読む|よむ|読みます))"
    rf"(?:って|と)?\s*(?:読む|よむ|読みます)"
)
_NATURAL_SURFACE_READING = re.compile(
    rf"(?P<surface>{_SURFACE})\s*{_NOT}[、,\s]*"
    rf"[「『]?(?P<reading>{_KANA_CHAR}{{2,32}}?)[」』]?"
    rf"(?:(?:って|と)?\s*(?:読む|よむ|読みます|発音(?:する|します)?)(?:んだよ)?)?"
    rf"(?=$|[。！？!?、,\s])"
)
_NATURAL_READING_ONLY = re.compile(
    rf"(?P<wrong>{_KANA_CHAR}{{2,}})\s*{_NOT}[、,\s]*"
    rf"[「『]?(?P<reading>{_KANA_CHAR}{{2,32}})[」』]?(?=$|[。！？!?、,\s])"
)
_CANONICALIZED_REPEAT = re.compile(
    rf"(?P<surface>[一-龥々〆ヶ][一-龥々〆ヶぁ-んァ-ヶー]{{0,20}}?)\s*"
    rf"{_NOT}[、,\s]*"
    rf"(?P<repeated>[一-龥々〆ヶ][一-龥々〆ヶぁ-んァ-ヶー]{{0,20}}?)(?=$|[。！？!?、,\s])"
)
_KANJI_WORD = re.compile(r"(?P<prefix>[ぁ-んァ-ヶー]{0,12})(?P<kanji>[一-龥々〆ヶ]{1,8})(?P<suffix>[ぁ-んァ-ヶー]{0,12})")

# Whisper occasionally normalizes both sides of a pronunciation correction to
# the same kanji spelling.  These are deliberately limited to readings that
# are unambiguous and commonly misread; arbitrary repeated kanji must never be
# auto-learned because the spoken reading is no longer available in the STT.
_CANONICAL_READINGS: dict[str, str] = {
    "深掘り": "ふかぼり",
}


def normalize_pronunciations(raw: dict | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for surface, reading in (raw or {}).items():
        surface, reading = str(surface).strip(), str(reading).strip()
        if not surface or not reading or len(surface) > 80 or len(reading) > 80:
            continue
        if not re.fullmatch(_KANA, reading):
            continue
        result[surface] = reading
    return result


def apply_pronunciations(text: str, pronunciations: dict[str, str]) -> str:
    """Replace only for synthesis; the displayed/transcript text stays unchanged."""
    out = text or ""
    for surface in sorted(pronunciations, key=len, reverse=True):
        out = out.replace(surface, pronunciations[surface])
    return out


def extract_explicit_pronunciation_correction(text: str) -> tuple[str, str] | None:
    """Accept only corrections that include both surface form and kana reading.

    A phrase such as ``そんなかぜじゃなくて、そんなふう`` lacks the written
    surface, so it is intentionally not persisted automatically.  The UI can
    register it safely as ``そんな風 -> そんなふう`` instead.
    """
    # Prefer the common spoken form "そんな風はそんなふうって読む".  The
    # older broad pattern can backtrack into a final single kanji and learn
    # only "風", losing the word boundary that the user actually corrected.
    natural = re.search(
        r"(?P<surface>[\u3040-\u30ff\u3400-\u9fff々ー]{2,40})\s*(?:の読み方)?は\s*"
        r"(?P<reading>[\u3040-\u30ffー]{2,32})\s*(?:って|と)\s*(?:読む|よむ)",
        text or "",
    )
    if natural:
        surface, reading = natural.group("surface").strip(), natural.group("reading").strip()
        if any("\u4e00" <= ch <= "\u9fff" for ch in surface):
            return surface, reading
    match = _EXPLICIT.search(text or "") or _READING_LABEL.search(text or "")
    if not match:
        return None
    surface, reading = match.group(1).strip(), match.group(2).strip()
    if not any("一" <= ch <= "龥" for ch in surface):
        return None
    return surface, reading


def infer_pronunciation_correction(text: str, last_assistant_text: str) -> tuple[str, str] | None:
    """Infer a reading-only correction from the immediately preceding reply.

    It only learns when a kanji surface candidate has a clearly better kana
    prefix/suffix match than every other candidate.  Ambiguous wording stays a
    normal conversation turn instead of corrupting the pronunciation lexicon.
    """
    explicit = extract_explicit_pronunciation_correction(text)
    if explicit:
        return explicit

    source = text or ""
    # Natural corrections are normally spoken as "Xじゃなくて、よみ" without
    # adding "って読む".  The previous implementation only accepted the latter.
    natural = _NATURAL_SURFACE_READING.search(source)
    if natural and len(source) <= 36 and natural.group("surface") in (last_assistant_text or ""):
        return natural.group("surface").strip(), natural.group("reading").strip()

    # If STT converted the intended kana back to the same surface form (for
    # example "深掘りじゃなくて深掘り"), recover only a vetted canonical
    # reading.  Unknown words remain a normal conversation turn rather than
    # saving a potentially wrong pronunciation.
    repeated = _CANONICALIZED_REPEAT.search(source)
    if repeated:
        surface = repeated.group("surface").strip()
        if surface == repeated.group("repeated").strip() and surface in _CANONICAL_READINGS:
            return surface, _CANONICAL_READINGS[surface]

    explicit_reading_only = _READING_ONLY.search(source)
    match = explicit_reading_only
    if match is None and len(source) <= 36:
        match = _NATURAL_READING_ONLY.search(source)
    if not match or not last_assistant_text:
        return None
    wrong, reading = match.group("wrong"), match.group("reading")
    candidates: set[str] = set()
    for item in _KANJI_WORD.finditer(last_assistant_text):
        prefix, kanji, suffix = item.group("prefix"), item.group("kanji"), item.group("suffix")
        candidates.update({prefix + kanji, kanji + suffix, prefix + kanji + suffix})
    scored: list[tuple[int, str]] = []
    for surface in candidates:
        skeleton = re.sub(r"[一-龥々〆ヶ]", "", surface)
        common = 0
        for a, b in zip(skeleton, wrong):
            if a != b:
                break
            common += 1
        suffix_common = 0
        for a, b in zip(reversed(skeleton), reversed(wrong)):
            if a != b:
                break
            suffix_common += 1
        score = common * 3 + suffix_common * 2
        if score >= 3:
            scored.append((score, surface))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], len(item[1])))
    # A single kanji run produces overlapping variants (e.g. 「そんな風」 and
    # 「そんな風に」).  Prefer the shortest equally matched surface; only reject
    # a true tie between separate candidates.
    if len(scored) > 1 and scored[0][0] == scored[1][0] and len(scored[0][1]) == len(scored[1][1]):
        return None
    return scored[0][1], reading
