from __future__ import annotations

import threading

import numpy as np

from neuro_voice.tts.base import TTSBackend
from neuro_voice.tts.switchable import SwitchableTTS


class _FakeTTS(TTSBackend):
    def __init__(self, value: float, gate: threading.Event | None = None):
        self.value = value
        self.gate = gate
        self.started = threading.Event()
        self.closed = False
        self._speaker = 1

    def synthesize(self, text, emotion=None, style=None):
        self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=2)
        return np.asarray([self.value], dtype=np.float32), 24000

    @property
    def speaker(self):
        return self._speaker

    def set_speaker(self, speaker):
        self._speaker = int(speaker)

    def list_speakers(self):
        return [{"id": self._speaker, "label": "fake"}]

    def close(self):
        self.closed = True


def test_replace_updates_shared_reference():
    old = _FakeTTS(1.0)
    new = _FakeTTS(2.0)
    tts = SwitchableTTS(old, "voicevox")
    tts.replace(new, "style_bert_vits2")

    wav, _ = tts.synthesize("hello")
    assert wav.tolist() == [2.0]
    assert tts.backend_name == "style_bert_vits2"
    assert old.closed is True


def test_retired_backend_closes_after_inflight_synthesis():
    gate = threading.Event()
    old = _FakeTTS(1.0, gate)
    new = _FakeTTS(2.0)
    tts = SwitchableTTS(old, "voicevox")
    thread = threading.Thread(target=lambda: tts.synthesize("in flight"))
    thread.start()
    assert old.started.wait(timeout=1)

    tts.replace(new, "style_bert_vits2")
    assert old.closed is False
    assert tts.synthesize("next")[0].tolist() == [2.0]

    gate.set()
    thread.join(timeout=1)
    assert old.closed is True


def test_master_volume_survives_backend_switch():
    old = _FakeTTS(0.4)
    new = _FakeTTS(0.4)
    tts = SwitchableTTS(old, "voicevox", volume=0.5)
    assert tts.synthesize("quiet")[0].tolist() == [0.20000000298023224]
    tts.replace(new, "style_bert_vits2")
    assert tts.volume == 0.5
    assert tts.synthesize("still quiet")[0].tolist() == [0.20000000298023224]
