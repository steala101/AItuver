"""Backend-facing contract for confidence-carrying transcription."""
from __future__ import annotations

import numpy as np

from neuro_voice.stt.base import Transcriber
from neuro_voice.stt.transcript import Transcript, detailed_transcribe


class LegacySTT(Transcriber):
    """A backend written before confidence existed."""

    def __init__(self, text="こんばんは"):
        self.text = text
        self.calls = 0

    def transcribe(self, audio, sample_rate=16000):
        self.calls += 1
        return self.text


class DuckTypedSTT:
    """Not a Transcriber subclass at all — only has transcribe()."""

    def transcribe(self, audio, sample_rate=16000):
        return "ダックタイプ"


class DetailedSTT(Transcriber):
    def __init__(self):
        self.bias = None

    def transcribe(self, audio, sample_rate=16000):
        return "詳細"

    def transcribe_detailed(self, audio, sample_rate=16000):
        return Transcript(text="詳細", avg_logprob=-0.1)

    def set_context_bias(self, initial_prompt="", hotwords=""):
        self.bias = (initial_prompt, hotwords)


AUDIO = np.zeros(16000, dtype=np.float32)


def test_legacy_backend_degrades_to_a_plain_transcript():
    stt = LegacySTT()
    result = detailed_transcribe(stt, AUDIO)
    assert isinstance(result, Transcript)
    assert result.text == "こんばんは"
    assert result.uncertain_words == []
    assert result.reliable is True


def test_duck_typed_backend_still_works():
    result = detailed_transcribe(DuckTypedSTT(), AUDIO)
    assert result.text == "ダックタイプ"


def test_detailed_backend_is_used_when_available():
    result = detailed_transcribe(DetailedSTT(), AUDIO)
    assert result.avg_logprob == -0.1


def test_context_bias_is_a_no_op_on_backends_that_ignore_it():
    stt = LegacySTT()
    assert stt.set_context_bias("プロンプト", "ホットワード") is None


def test_context_bias_reaches_a_backend_that_supports_it():
    stt = DetailedSTT()
    stt.set_context_bias("プロンプト", "ホットワード")
    assert stt.bias == ("プロンプト", "ホットワード")


def test_partial_transcription_defaults_to_the_final_path():
    stt = LegacySTT()
    assert stt.transcribe_partial(AUDIO) == "こんばんは"
    assert stt.calls == 1
