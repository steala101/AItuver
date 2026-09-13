"""Build STT while keeping Whisper as the reliable default/fallback."""
from __future__ import annotations

import logging

from neuro_voice.stt.faster_whisper_stt import FasterWhisperSTT

logger = logging.getLogger(__name__)


def build_stt(cfg, *, device: str | None = None):
    """Return the synchronous Transcriber used by the existing realtime path."""
    s = cfg.section("stt")
    selected = str(cfg.get("audio_input.backend", s.get("backend", "whisper"))).lower()
    fallback = str(cfg.get("audio_input.fallback_backend", "whisper")).lower()
    if selected == "gemma4_audio":
        # Native audio is not exposed through the implemented Ollama/GGUF
        # adapter. Do not pretend it works: select the configured Whisper
        # fallback before capturing a live turn.
        logger.warning(
            "Gemma 4 audio input is unavailable through the current Ollama backend. Falling back to %s.",
            fallback,
        )
    return FasterWhisperSTT(
        model=s.get("model", "large-v3-turbo"),
        device=device or s.get("device", "cuda"),
        compute_type=s.get("compute_type", "float16"),
        language=cfg.get("audio_input.language", s.get("language")),
        download_root=s.get("download_root", "models"),
        cpu_threads=int(s.get("cpu_threads", 8)), cpu_model=s.get("cpu_model"),
        beam_size=int(s.get("beam_size", 5)), initial_prompt=s.get("initial_prompt"),
        hotwords=s.get("hotwords"), corrections=s.get("corrections") or {},
        wake_word_aliases=s.get("wake_word_aliases") or {},
        partial_beam_size=(int(s["partial_beam_size"]) if s.get("partial_beam_size") is not None else 1),
        final_beam_size=(int(s["final_beam_size"]) if s.get("final_beam_size") is not None else None),
        word_confidence=bool(cfg.get("stt.transcript_repair.word_confidence", True)),
        temperature_fallback=bool(s.get("temperature_fallback", True)),
        repetition_penalty=float(s.get("repetition_penalty", 1.0)),
        no_repeat_ngram_size=int(s.get("no_repeat_ngram_size", 0)),
        keep_fillers=bool(s.get("keep_fillers", True)),
    )


def build_transcript_repairer(cfg):
    """Build the shared homophone repairer, or ``None`` when disabled."""
    from neuro_voice.stt.transcript_repair import TranscriptRepairer

    section = cfg.section("stt").get("transcript_repair") or {}
    if not bool(section.get("enabled", True)):
        return None
    return TranscriptRepairer(
        max_terms=int(section.get("max_context_terms", 24)),
        max_bias_terms=int(section.get("max_bias_terms", 8)),
        word_confidence_threshold=float(section.get("word_confidence_threshold", 0.60)),
        utterance_logprob_threshold=float(section.get("utterance_logprob_threshold", -0.70)),
        auto_repair=bool(section.get("auto_repair", True)),
        collapse_stutter=bool(section.get("collapse_stutter", True)),
        user_readings=cfg.get("tts.pronunciations") or {},
    )
