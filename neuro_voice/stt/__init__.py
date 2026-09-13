"""Audio-input backends."""

from neuro_voice.stt.audio_understanding import AudioUnderstandingResult, Gemma4AudioBackend, WhisperAudioBackend
from neuro_voice.stt.factory import build_stt, build_transcript_repairer
from neuro_voice.stt.transcript import (
    Transcript, TranscriptRepairNote, WordConfidence, detailed_transcribe,
)
from neuro_voice.stt.transcript_repair import (
    ContextVocabulary, TranscriptRepairer, asr_uncertainty_prompt,
)

__all__ = [
    "AudioUnderstandingResult", "Gemma4AudioBackend", "WhisperAudioBackend",
    "build_stt", "build_transcript_repairer",
    "Transcript", "TranscriptRepairNote", "WordConfidence", "detailed_transcribe",
    "ContextVocabulary", "TranscriptRepairer", "asr_uncertainty_prompt",
]
