"""Noticing a headset that was switched on after start-up.

Observed: with the headset powered off at launch, turning it on later did
nothing — neither microphone nor speaker was picked up, and the only remedy
was restarting the application.
"""
from __future__ import annotations

import pytest

from neuro_voice.audio.devices import (
    AudioDeviceMonitor, DeviceSnapshot, read_snapshot, refresh_device_list,
)


class FakeSoundDevice:
    """Just enough of the sounddevice surface to drive the monitor."""

    class _Default:
        device = (None, None)

    def __init__(self, devices=None, default=(None, None)):
        self._devices = list(devices or [])
        self.default = self._Default()
        self.default.device = default
        self.refreshes = 0
        self.fail_query = False

    def query_devices(self, index=None):
        if self.fail_query:
            raise OSError("no audio backend")
        if index is None:
            return list(self._devices)
        if index is None or not isinstance(index, int):
            raise ValueError("invalid device")
        return self._devices[index]

    def _terminate(self):
        self.refreshes += 1

    def _initialize(self):
        pass

    # -- test helpers --
    def plug_headset(self):
        self._devices.append(
            {"name": "Headset Mic", "hostapi": 0, "max_input_channels": 1},
        )
        self._devices.append(
            {"name": "Headset Out", "hostapi": 0, "max_output_channels": 2},
        )
        self.default.device = (len(self._devices) - 2, len(self._devices) - 1)


def laptop():
    return FakeSoundDevice(
        devices=[
            {"name": "Internal Mic", "hostapi": 0, "max_input_channels": 1},
            {"name": "Internal Speaker", "hostapi": 0, "max_output_channels": 2},
        ],
        default=(0, 1),
    )


def silent_machine():
    return FakeSoundDevice(devices=[], default=(None, None))


# ---------------------------------------------------------------------------
# Reading the current hardware
# ---------------------------------------------------------------------------


def test_a_machine_with_no_audio_reads_as_empty():
    snapshot = read_snapshot(silent_machine())
    assert snapshot.has_input is False
    assert snapshot.input_count == 0


def test_the_current_defaults_are_named():
    snapshot = read_snapshot(laptop())
    assert "Internal Mic" in snapshot.input_name
    assert "Internal Speaker" in snapshot.output_name
    assert snapshot.input_count == 1


def test_a_broken_backend_does_not_raise():
    sd = laptop()
    sd.fail_query = True
    snapshot = read_snapshot(sd)
    assert snapshot == DeviceSnapshot()


def test_refreshing_rebuilds_the_cached_list():
    sd = laptop()
    assert refresh_device_list(sd) is True
    assert sd.refreshes == 1


def test_refreshing_is_optional():
    class Minimal:
        pass

    assert refresh_device_list(Minimal()) is False


# ---------------------------------------------------------------------------
# Detecting the change
# ---------------------------------------------------------------------------


def test_the_first_poll_is_a_baseline_not_an_event():
    sd = laptop()
    monitor = AudioDeviceMonitor(sd)
    reason, _snapshot = monitor.poll()
    assert reason == "initial"
    assert sd.refreshes == 0


def test_no_change_reports_nothing():
    monitor = AudioDeviceMonitor(laptop())
    monitor.poll()
    assert monitor.poll()[0] == ""


def test_a_headset_switched_on_later_is_noticed():
    """The whole point: the device did not exist when the app started."""
    sd = silent_machine()
    monitor = AudioDeviceMonitor(sd)
    reason, snapshot = monitor.poll()
    assert reason == "initial"
    assert snapshot.has_input is False

    sd.plug_headset()
    reason, snapshot = monitor.poll()
    assert reason == "input_changed"
    assert "Headset Mic" in snapshot.input_name


def test_switching_from_one_device_to_another_is_noticed():
    sd = laptop()
    monitor = AudioDeviceMonitor(sd)
    monitor.poll()
    sd.plug_headset()
    assert monitor.poll()[0] == "input_changed"


def test_an_output_only_change_is_reported_separately():
    sd = laptop()
    monitor = AudioDeviceMonitor(sd)
    monitor.poll()
    sd._devices.append({"name": "USB Speaker", "hostapi": 0, "max_output_channels": 2})
    sd.default.device = (0, len(sd._devices) - 1)
    assert monitor.poll()[0] == "output_changed"


def test_polling_can_skip_the_rebuild():
    sd = laptop()
    AudioDeviceMonitor(sd).poll(refresh=False)
    assert sd.refreshes == 0


def test_polling_refresh_is_explicit_because_it_stops_live_streams():
    sd = laptop()
    AudioDeviceMonitor(sd).poll(refresh=True)
    assert sd.refreshes == 1


def test_the_monitor_survives_a_backend_failure():
    sd = laptop()
    monitor = AudioDeviceMonitor(sd)
    monitor.poll()
    sd.fail_query = True
    reason, snapshot = monitor.poll()
    assert reason == "input_changed"     # everything vanished; that is a change
    assert snapshot.has_input is False


# ---------------------------------------------------------------------------
# Comparison rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("previous,expected", [
    (None, "initial"),
    (DeviceSnapshot("Headset#0", "Out#0", 1), ""),
    (DeviceSnapshot("Other#0", "Out#0", 1), "input_changed"),
    (DeviceSnapshot("Headset#0", "Other#0", 1), "output_changed"),
    (DeviceSnapshot("", "Out#0", 0), "input_changed"),
])
def test_change_reasons(previous, expected):
    current = DeviceSnapshot("Headset#0", "Out#0", 1)
    assert current.differs_from(previous) == expected
