"""Audio-understanding backend contract and safe Gemma fallback policy."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from neuro_voice.stt.base import Transcriber

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AudioUnderstandingResult:
    text: str
    language: str | None = None
    confidence: float | None = None
    emotion: str | None = None
    non_speech_events: list[str] = field(default_factory=list)
    backend: str = "whisper"


class AudioUnderstandingBackend(Protocol):
    async def transcribe(
        self, audio: bytes, sample_rate: int, language: str | None = None,
    ) -> AudioUnderstandingResult: ...


class WhisperAudioBackend:
    """Adapter around the existing faster-whisper implementation."""

    name = "whisper"

    def __init__(self, transcriber: Transcriber):
        self._transcriber = transcriber

    async def transcribe(self, audio: bytes, sample_rate: int, language: str | None = None) -> AudioUnderstandingResult:
        pcm = np.frombuffer(audio, dtype=np.float32)
        text = await asyncio.to_thread(self._transcriber.transcribe, pcm, sample_rate)
        return AudioUnderstandingResult(text=text, language=language, backend=self.name)


class Gemma4AudioBackend:
    """Explicitly unavailable until an Ollama transport exposes native audio.

    The official model capability must be verified at runtime.  This class does
    not send WAV bytes as a fake image or claim success from text-only Ollama.
    """

    name = "gemma4_audio"

    def __init__(self, model: str, fallback: AudioUnderstandingBackend):
        self.model = model
        self._fallback = fallback
        self.available = False
        self._warned = False

    async def transcribe(self, audio: bytes, sample_rate: int, language: str | None = None) -> AudioUnderstandingResult:
        if not self._warned:
            logger.warning(
                "Gemma 4 audio input is unavailable through the current Ollama backend. Falling back to Whisper."
            )
            self._warned = True
        result = await self._fallback.transcribe(audio, sample_rate, language)
        return AudioUnderstandingResult(
            text=result.text, language=result.language, confidence=result.confidence,
            emotion=result.emotion, non_speech_events=result.non_speech_events,
            backend="whisper_fallback",
        )
