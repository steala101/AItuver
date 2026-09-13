"""Repair speech recognition output using the conversation, not a dictionary.

Japanese speech recognition fails in a specific way: the sounds are right and
the kanji are wrong.  "毎クラ" for "マイクラ", "小水" for "お水".  A model that
treats the written form as authoritative will then reason about the wrong word
and answer something that makes no sense.

Three defences, cheapest first:

1. Bias the recognizer itself with what this conversation is actually about, so
   the mistake is never made (``ContextVocabulary`` -> initial_prompt/hotwords).
2. Silently repair a low-confidence word whose *reading* exactly matches a term
   already present in the conversation.
3. Tell the model which words were unreliable, so it reinterprets by sound
   instead of forcing the kanji meaning.

No extra model call is involved in any of them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from neuro_voice.activity.japanese_reading import (
    JapaneseReadingService, katakana_to_hiragana, normalize_kana,
)
from neuro_voice.stt.transcript import Transcript, TranscriptRepairNote, WordConfidence

logger = logging.getLogger(__name__)

_KANJI = re.compile(r"[一-龥々〆ヶ]")
#: Japanese has no spaces, so a run of "kanji or kana" swallows whole clauses
#: ("ポッポは自分の声聞こえる").  Splitting by script keeps word-like pieces.
_TOKEN = re.compile(
    r"[一-龥々〆ヶ]{1,6}"        # kanji compound
    r"|[ァ-ヺ][ァ-ヺー]{1,11}"    # katakana word
    r"|[A-Za-z][A-Za-z0-9]{1,15}"  # latin word
)
_PARTICLE = re.compile(
    r"^(?:です|ます|でした|ました|ですね|だよね|かな|けど|から|ので|"
    r"って|という|といった|みたいな|そして|でも|それで|あと|ちょっと|"
    r"すごく|とても|本当に|やっぱり|たぶん|なんか|えっと|あの|その)$"
)
#: Words too common to be worth biasing or repairing toward.
_STOPWORDS = frozenset({
    "こと", "もの", "とき", "ところ", "ため", "はなし", "話", "自分", "感じ",
    "今日", "明日", "昨日", "今", "人", "気", "方", "的", "会話", "音声",
    "する", "した", "してる", "なる", "なった", "ある", "いる", "思う",
})


def _clean_token(value: str) -> str:
    return str(value or "").strip(" 　、。,.!！?？「」『』\"'()（）・…\n\t")


#: A doubled short word is ordinary Japanese emphasis ("そうそう", "はいはい"),
#: so a short phrase needs three or more occurrences to count as a loop.
_STUTTER_SHORT = re.compile(r"(.{2,5}?)(?:[、,\s]*\1){2,}")
#: A long phrase repeating even once is not something people say.
#: "仕事ではよく使って、仕事ではよく使ってる"
_STUTTER_LONG = re.compile(r"(.{6,24}?)(?:[、,\s]*\1){1,}")


def collapse_repetitions(text: str, *, max_passes: int = 3) -> tuple[str, int]:
    """Collapse an immediately repeated phrase down to one occurrence.

    Only *adjacent* repeats are touched, and the bar depends on length: people
    really do say "そうそう", but nobody says "最初は、最初は、最初は".
    """
    value = str(text or "")
    if not value:
        return value, 0
    collapsed = 0
    for _ in range(max_passes):
        result = value
        count = 0
        for pattern in (_STUTTER_LONG, _STUTTER_SHORT):
            result, hits = pattern.subn(r"\1", result)
            count += hits
        if not count or result == value:
            break
        collapsed += count
        value = result
    return value, collapsed


@dataclass(slots=True)
class ContextVocabulary:
    """Two vocabularies, deliberately kept apart.

    ``bias_terms`` reach the recognizer and change what it hears.  Only
    verified, stable proper nouns belong there: names and the current activity.
    Feeding it free text harvested from previous transcripts creates a loop
    where one mis-recognition becomes a biasing term and reinforces itself, and
    an over-long hotword list makes Whisper *insert* those words outright.

    ``repair_terms`` never touch decoding.  They are only compared by reading
    after the fact, so they can be broad and occasionally wrong at no cost.
    """

    max_terms: int = 24
    max_bias_terms: int = 8
    bias_terms: list[str] = field(default_factory=list)
    repair_terms: list[str] = field(default_factory=list)
    #: reading -> surfaces, for repairing a homophone back to a known term.
    _by_reading: dict[str, list[str]] = field(default_factory=dict)

    @property
    def terms(self) -> list[str]:
        """Every term available for post-hoc repair."""
        return list(dict.fromkeys([*self.bias_terms, *self.repair_terms]))

    def snapshot(self) -> dict[str, Any]:
        return {
            "bias_terms": list(self.bias_terms),
            "repair_terms": list(self.repair_terms[:12]),
            "readings": len(self._by_reading),
        }

    def surfaces_for(self, reading: str) -> list[str]:
        return list(self._by_reading.get(reading, ()))

    def initial_prompt(self, base: str = "", *, include_terms: bool = False) -> str:
        """Prompt text handed to Whisper before decoding this utterance.

        Whisper treats ``initial_prompt`` as preceding transcript text and will
        happily emit parts of it.  A meta sentence listing current topics is
        therefore off by default: it leaked biasing words straight into the
        transcript ("さあ、ib ポッポ、ポッポ、チビを…").
        """
        base = str(base or "").strip()
        if not include_terms or not self.bias_terms:
            return base
        topic = "、".join(self.bias_terms[:6])
        note = f"{topic}について話しています。"
        return f"{base} {note}".strip() if base else note

    def hotwords(self, base: str = "") -> str:
        """A short hotword list.  Long lists distort decoding badly."""
        values = [item for item in str(base or "").split() if item]
        for term in self.bias_terms:
            if term not in values:
                values.append(term)
        return " ".join(values[: max(1, self.max_bias_terms)])


class TranscriptRepairer:
    """Build biasing vocabulary and repair obvious homophone damage."""

    def __init__(
        self,
        *,
        max_terms: int = 24,
        max_bias_terms: int = 8,
        word_confidence_threshold: float = 0.60,
        utterance_logprob_threshold: float = -0.70,
        auto_repair: bool = True,
        collapse_stutter: bool = True,
        user_readings: dict[str, str] | None = None,
    ) -> None:
        self.max_terms = max(4, int(max_terms))
        # A long hotword list does not "help a bit more"; it makes Whisper
        # insert those words into unrelated speech.  Keep this small.
        self.max_bias_terms = max(0, min(12, int(max_bias_terms)))
        self.word_confidence_threshold = float(word_confidence_threshold)
        self.utterance_logprob_threshold = float(utterance_logprob_threshold)
        self.auto_repair = bool(auto_repair)
        self.collapse_stutter = bool(collapse_stutter)
        self._readings = JapaneseReadingService(user_readings)
        self._reading_backend_available = self._readings._backend is not None

    # -- vocabulary ------------------------------------------------------
    def build_vocabulary(
        self,
        *,
        recent_turns: Iterable[str] = (),
        topics: Iterable[str] = (),
        proper_nouns: Iterable[str] = (),
        memory_terms: Iterable[str] = (),
        activity_terms: Iterable[str] = (),
        interests: Iterable[str] = (),
    ) -> ContextVocabulary:
        """Split context into what may steer the recognizer and what may not."""
        vocabulary = ContextVocabulary(
            max_terms=self.max_terms, max_bias_terms=self.max_bias_terms,
        )

        def collect(values: Iterable[str], *, tokenize: bool) -> list[str]:
            found: list[str] = []
            for raw in values:
                candidates = (
                    self._tokens(str(raw)) if tokenize else [_clean_token(str(raw))]
                )
                for term in candidates:
                    if self._is_useful_term(term) and term not in found:
                        found.append(term)
            return found

        # Only names and the current activity may steer decoding.  They are
        # user-confirmed, stable, and exactly what a general recognizer gets
        # wrong.  Nothing derived from a previous transcript goes here, or a
        # mis-recognition would bias the next decode toward repeating itself.
        bias = collect(proper_nouns, tokenize=False)
        for term in collect(activity_terms, tokenize=False):
            if term not in bias:
                bias.append(term)
        vocabulary.bias_terms = bias[: self.max_bias_terms]

        # Everything else is repair-only: compared by reading after decoding,
        # so a wrong entry costs nothing.
        repair: list[str] = []
        for values, tokenize in (
            (topics, False), (interests, False),
            (recent_turns, True), (memory_terms, True),
        ):
            for term in collect(values, tokenize=tokenize):
                if term not in repair and term not in vocabulary.bias_terms:
                    repair.append(term)
        vocabulary.repair_terms = repair[: self.max_terms]

        for term in vocabulary.terms:
            reading = self._reading_of(term)
            if reading:
                vocabulary._by_reading.setdefault(reading, []).append(term)
        return vocabulary

    def _tokens(self, text: str) -> list[str]:
        return [_clean_token(match) for match in _TOKEN.findall(str(text or ""))]

    def _is_useful_term(self, term: str) -> bool:
        if not term or len(term) < 2 or len(term) > 16:
            return False
        if term in _STOPWORDS or _PARTICLE.fullmatch(term):
            return False
        return True

    def _reading_of(self, surface: str) -> str:
        value = _clean_token(surface)
        if not value:
            return ""
        if not _KANJI.search(value):
            # Kana and latin need no backend; normalize katakana to hiragana so
            # "マイクラ" and "まいくら" compare equal.
            return normalize_kana(katakana_to_hiragana(value)) or value.lower()
        if not self._reading_backend_available:
            return ""
        return self._readings.reading(value)

    # -- repair ----------------------------------------------------------
    def repair(
        self, transcript: Transcript, vocabulary: ContextVocabulary | None,
    ) -> Transcript:
        """Mark unreliable words and apply only unambiguous repairs."""
        if not transcript.text:
            return transcript
        if self.collapse_stutter:
            collapsed, count = collapse_repetitions(transcript.text)
            if count:
                transcript.repairs.append(TranscriptRepairNote(
                    original=transcript.text[:80], corrected=collapsed[:80],
                    reason="decoder_stutter_collapsed",
                ))
                logger.info(
                    "STT反復を圧縮 (%d箇所): %r → %r",
                    count, transcript.text[:60], collapsed[:60],
                )
                transcript.text = collapsed
                # Word offsets no longer line up with the rewritten text.
                transcript.words = []
        if vocabulary is not None and vocabulary.bias_terms:
            cleaned = self._remove_context_bias_echo(
                transcript.text, vocabulary.bias_terms,
            )
            if cleaned != transcript.text:
                original = transcript.text
                transcript.text = cleaned
                transcript.words = []
                transcript.repairs.append(TranscriptRepairNote(
                    original=original[-80:], corrected=cleaned[-80:],
                    reason="context_bias_echo_removed",
                ))
                logger.info(
                    "STT文脈語の末尾混入を除去: %r → %r",
                    original[-100:], cleaned[-100:],
                )
        transcript.uncertain_words = self._uncertain(transcript)
        if vocabulary is None or not vocabulary.terms:
            return transcript
        for word in list(transcript.uncertain_words):
            surface = _clean_token(word.clean)
            if not surface or surface in vocabulary.terms:
                continue
            reading = self._reading_of(surface)
            if not reading:
                continue
            matches = [item for item in vocabulary.surfaces_for(reading) if item != surface]
            if not matches:
                continue
            if len(matches) > 1:
                # Two context words share this reading.  Guessing would just
                # move the error, so hand both to the model instead.
                transcript.candidates[surface] = matches[:3]
                continue
            if not self.auto_repair:
                transcript.candidates[surface] = matches[:1]
                continue
            corrected = matches[0]
            transcript.text = transcript.text.replace(surface, corrected)
            transcript.repairs.append(TranscriptRepairNote(
                original=surface, corrected=corrected,
                reason="context_reading_match", reading=reading,
            ))
            transcript.uncertain_words = [
                item for item in transcript.uncertain_words if item is not word
            ]
            logger.info(
                "STT文脈補正: %r → %r (読み=%s)", surface, corrected, reading,
            )
        return transcript

    @staticmethod
    def _remove_context_bias_echo(text: str, bias_terms: Iterable[str]) -> str:
        """Remove an appended hotword list without touching normal name use.

        Whisper occasionally returns a good sentence followed by three or more
        context hotwords.  Requiring multiple *exact* terms and restricting the
        repair to the tail keeps this much safer than a general text rewrite.
        """
        value = str(text or "").strip()
        terms = [
            str(term).strip() for term in bias_terms
            if len(str(term).strip()) >= 2
        ]
        if not value or len(terms) < 2:
            return value

        # Strong case: a complete sentence/question followed by a compact list.
        punctuation = list(re.finditer(r"[。！？!?]", value))
        if punctuation:
            boundary = punctuation[-1].end()
            tail = value[boundary:].strip(" 　、,・")
            hits = {term for term in terms if term in tail}
            if tail and len(tail) <= 80 and len(hits) >= 3:
                return value[:boundary].strip()

        # Some decoders omit punctuation but separate injected hotwords with
        # spaces.  Strip only a suffix made mostly from exact bias terms.
        tokens = value.split()
        if len(tokens) >= 3:
            suffix_hits = 0
            cut = len(tokens)
            for index in range(len(tokens) - 1, 0, -1):
                token = _clean_token(tokens[index])
                if any(term == token for term in terms):
                    suffix_hits += 1
                    cut = index
                    continue
                break
            if suffix_hits >= 3 and cut > 0:
                return " ".join(tokens[:cut]).rstrip(" 　、,・")
        return value

    def _uncertain(self, transcript: Transcript) -> list[WordConfidence]:
        weak_utterance = transcript.avg_logprob < self.utterance_logprob_threshold
        threshold = self.word_confidence_threshold + (0.12 if weak_utterance else 0.0)
        uncertain: list[WordConfidence] = []
        for word in transcript.words:
            surface = _clean_token(word.clean)
            if not surface or len(surface) < 2:
                continue
            if _PARTICLE.fullmatch(surface) or surface in _STOPWORDS:
                continue
            if word.probability >= threshold:
                continue
            if not _KANJI.search(surface) and not re.search(r"[ァ-ヺ]", surface):
                # A short hiragana run at low confidence is almost always a
                # filler.  A longer one can still be a content word written in
                # the wrong script ("まいくら" for "マイクラ"), so keep those.
                if len(surface) < 3:
                    continue
            uncertain.append(word)
        return uncertain[:4]


def asr_uncertainty_prompt(
    transcript: Transcript | None, *, ask_when_stuck: bool = True,
) -> str | None:
    """Per-turn note telling the model its input is a recognition hypothesis.

    Without this the persona's factuality guard actively works against us: it
    tells the model not to invent meanings for unknown words, so the model
    instead treats a mis-converted kanji as a real word and reasons from it.
    """
    if transcript is None or not transcript.text.strip():
        return None
    uncertain = transcript.uncertain_surfaces
    candidates = transcript.candidates
    weak = transcript.avg_logprob < -0.70
    if not uncertain and not candidates and not weak:
        return None

    lines = [
        "【今回の入力についての注意（音声認識）】",
        "ユーザーの発話は音声認識の結果であり、同音異義語の変換ミスが起こりうる。",
    ]
    if uncertain:
        lines.append(
            "聴き取りが不確かな語: " + "、".join(f"「{item}」" for item in uncertain[:4])
        )
    if candidates:
        lines.append(
            "文脈から考えられる別解: " + " / ".join(
                f"「{surface}」→ {'、'.join(values[:3])}"
                for surface, values in list(candidates.items())[:3]
            )
        )
    if weak and not uncertain:
        lines.append("発話全体の認識確度が低い。細部の語形は当てにしない。")
    lines.extend([
        "扱い方:",
        "- 文脈に対して意味が通らない語は、その漢字の意味で解釈しない。"
        "音（読み）が同じ別の言葉が本来の発話だと考え、直前の話題・場面から自然に当てはまる語へ読み替える。",
        "- 読み替えた結果で普通に会話を続ける。認識ミスや変換ミスの話題そのものには触れない。"
        "「音声認識が」「誤変換が」のようなメタ発言はしない。",
        "- 語形が不確かでも、発話全体の意図はたいてい取れる。意図が取れるなら止まらずに答える。",
    ])
    if ask_when_stuck:
        lines.append(
            "- どう読み替えても意図が取れない時だけ、聞き取れなかった部分を"
            "「〇〇のところ、もう一回いい？」のように一言で短く聞き返す。毎回は確認しない。"
        )
    else:
        lines.append(
            "- 意図が取れない場合も聞き返さず、最も自然な解釈で受け答えする。"
        )
    return "\n".join(lines)
