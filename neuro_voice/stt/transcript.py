"""Speech recognition results that keep their uncertainty.

A transcript is a hypothesis, not a fact.  Throwing away the recognizer's
confidence and handing downstream code a bare string is what makes an assistant
take a mis-heard homophone literally and answer something meaningless.

This module carries that uncertainty forward: which words were unreliable, how
unreliable the utterance was overall, and what was silently repaired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class WordConfidence:
    """One recognized word and how much the recognizer believed it."""

    word: str
    probability: float = 1.0
    start: float = 0.0
    end: float = 0.0

    @property
    def clean(self) -> str:
        return self.word.strip()


@dataclass(slots=True)
class TranscriptRepairNote:
    """One silent correction, kept so it stays auditable."""

    original: str
    corrected: str
    reason: str
    reading: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "original": self.original, "corrected": self.corrected,
            "reason": self.reason, "reading": self.reading,
        }


@dataclass(slots=True)
class Transcript:
    """A recognition hypothesis with its confidence intact."""

    text: str = ""
    language: str = ""
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 0.0
    words: list[WordConfidence] = field(default_factory=list)
    #: Words whose probability fell below the configured threshold.
    uncertain_words: list[WordConfidence] = field(default_factory=list)
    #: Corrections already applied to ``text``.
    repairs: list[TranscriptRepairNote] = field(default_factory=list)
    #: Alternatives offered to the model but not applied.
    candidates: dict[str, list[str]] = field(default_factory=dict)

    def __str__(self) -> str:      # keeps `str(result)` working everywhere
        return self.text

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    @property
    def reliable(self) -> bool:
        return not self.uncertain_words and self.avg_logprob > -0.55

    @property
    def uncertain_surfaces(self) -> list[str]:
        seen: list[str] = []
        for item in self.uncertain_words:
            value = item.clean
            if value and value not in seen:
                seen.append(value)
        return seen

    def snapshot(self, *, include_text: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "avg_logprob": round(self.avg_logprob, 3),
            "no_speech_prob": round(self.no_speech_prob, 3),
            "reliable": self.reliable,
            "uncertain": self.uncertain_surfaces,
            "repairs": [item.snapshot() for item in self.repairs],
            "candidates": {k: list(v) for k, v in self.candidates.items()},
        }
        if include_text:
            value["text"] = self.text
        return value

    @classmethod
    def plain(cls, text: str) -> "Transcript":
        """Wrap a backend that cannot report confidence at all."""
        return cls(text=str(text or "").strip())


def detailed_transcribe(stt: Any, audio: Any, sample_rate: int = 16000) -> Transcript:
    """Get a confidence-carrying transcript from any backend.

    A custom backend that only implements ``transcribe`` still works; it simply
    reports nothing as uncertain, which degrades to the previous behaviour.
    """
    method = getattr(stt, "transcribe_detailed", None)
    if callable(method):
        result = method(audio, sample_rate)
        if isinstance(result, Transcript):
            return result
        return Transcript.plain(str(result or ""))
    return Transcript.plain(stt.transcribe(audio, sample_rate))


def collect_words(segments: Iterable[Any]) -> tuple[str, list[WordConfidence], float, float, float]:
    """Read faster-whisper segments once, keeping per-word probability."""
    parts: list[str] = []
    words: list[WordConfidence] = []
    logprob_sum = 0.0
    no_speech = 0.0
    compression = 0.0
    count = 0
    for segment in segments:
        parts.append(str(getattr(segment, "text", "") or ""))
        count += 1
        logprob_sum += float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        no_speech = max(no_speech, float(getattr(segment, "no_speech_prob", 0.0) or 0.0))
        compression = max(
            compression, float(getattr(segment, "compression_ratio", 0.0) or 0.0)
        )
        for word in getattr(segment, "words", None) or ():
            surface = str(getattr(word, "word", "") or "")
            if not surface.strip():
                continue
            words.append(WordConfidence(
                word=surface,
                probability=float(getattr(word, "probability", 1.0) or 0.0),
                start=float(getattr(word, "start", 0.0) or 0.0),
                end=float(getattr(word, "end", 0.0) or 0.0),
            ))
    text = "".join(parts).strip()
    avg_logprob = logprob_sum / count if count else 0.0
    return text, words, avg_logprob, no_speech, compression
