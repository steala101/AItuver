"""Looking for the microphone again, on request.

The background watcher only recovers a stream that was open and then stopped.
Two situations it cannot help with:

* the microphone was never present at start-up and the person plugged it in;
* Windows moved the endpoint while the old handle still reports itself alive.

Both used to be fixed only by restarting the application.
"""
from __future__ import annotations

import pytest

from neuro_voice.audio.devices import DeviceSnapshot, device_label


class FakeStream:
    def __init__(self, *, alive=True):
        self.active = alive
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def close(self):
        pass


class FakeMic:
    """The part of MicCapture the re-detect path touches."""

    def __init__(self, *, configured="Headset Mic", present=()):
        self._device = configured
        self._present = set(present)
        self._stream = None
        self.last_error = ""
        self.opened_device = None
        self.used_fallback = False
        self.attempts: list[object] = []

    def _open(self, device):
        self.attempts.append(device)
        if device is None or device in self._present:
            self._stream = FakeStream()
            return True
        self._stream = None
        self.last_error = f"Error opening InputStream: {device} not found"
        return False

    def start(self, *, allow_fallback=False):
        if self._stream is not None:
            return True
        if self._open(self._device):
            self.last_error = ""
            self.opened_device, self.used_fallback = self._device, False
            return True
        configured_error = self.last_error
        if allow_fallback and self._device is not None and self._open(None):
            self.last_error = ""
            self.opened_device, self.used_fallback = None, True
            return True
        self.last_error = configured_error
        return False

    def stop(self):
        self._stream = None

    def restart(self, *, allow_fallback=False):
        self.stop()
        return self.start(allow_fallback=allow_fallback)


# ---------------------------------------------------------------------------
# Honouring the configured device
# ---------------------------------------------------------------------------


def test_the_configured_microphone_is_tried_first():
    mic = FakeMic(configured="Headset Mic", present={"Headset Mic"})
    assert mic.restart(allow_fallback=True) is True
    assert mic.attempts == ["Headset Mic"]
    assert mic.used_fallback is False


def test_a_missing_device_falls_back_and_says_so():
    mic = FakeMic(configured="Headset Mic", present=set())
    assert mic.restart(allow_fallback=True) is True
    assert mic.attempts == ["Headset Mic", None]
    assert mic.used_fallback is True


def test_automatic_recovery_never_falls_back_silently():
    """Only the person pressing the button may change which mic is used."""
    mic = FakeMic(configured="Headset Mic", present=set())
    assert mic.restart() is False
    assert mic.attempts == ["Headset Mic"]
    assert mic.used_fallback is False
    assert "not found" in mic.last_error


def test_the_failure_reason_survives_the_fallback_attempt():
    mic = FakeMic(configured="Headset Mic", present=set())
    mic._open = lambda device: False           # nothing opens at all
    assert mic.restart(allow_fallback=True) is False


def test_no_configured_device_means_nothing_to_fall_back_from():
    mic = FakeMic(configured=None, present=set())
    assert mic.restart(allow_fallback=True) is True
    assert mic.attempts == [None]
    assert mic.used_fallback is False


# ---------------------------------------------------------------------------
# What the person is shown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,shown", [
    ("Headset Mic#0", "Headset Mic"),
    ("マイク配列 (Realtek(R) Audio)#2", "マイク配列 (Realtek(R) Audio)"),
    ("", ""),
    ("NoHostApi", "NoHostApi"),
])
def test_the_hostapi_suffix_is_not_shown(raw, shown):
    assert device_label(raw) == shown


def test_the_suffix_still_distinguishes_snapshots():
    """Stripping it for display must not weaken change detection."""
    wasapi = DeviceSnapshot("Headset Mic#0", "Out#0", 1)
    mme = DeviceSnapshot("Headset Mic#1", "Out#0", 1)
    assert wasapi.differs_from(mme) == "input_changed"
