"""Noticing that the audio hardware changed.

A headset that is switched on after the app started does not exist as far as
the already-running audio streams are concerned, and PortAudio caches its
device list at initialisation — so the new device is invisible until that list
is rebuilt.  Without this the only remedy is restarting the whole application.

The comparison logic here is pure so it can be tested without a sound card;
the PortAudio calls are kept to the thinnest possible wrapper.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Identity of the devices currently in use, as names rather than indexes.

    Indexes are reassigned when hardware appears or disappears, so comparing
    them reports a change that did not happen — and misses one that did.
    """

    input_name: str = ""
    output_name: str = ""
    input_count: int = 0

    @property
    def has_input(self) -> bool:
        return bool(self.input_name)

    def differs_from(self, other: "DeviceSnapshot | None") -> str:
        """Return a reason code when the usable hardware has changed."""
        if other is None:
            return "initial"
        if self.input_name != other.input_name:
            return "input_changed"
        if self.output_name != other.output_name:
            return "output_changed"
        if self.has_input and not other.has_input:
            return "input_appeared"
        return ""


def _name_of(sd_module: Any, index: Any) -> str:
    try:
        info = sd_module.query_devices(index)
    except Exception:
        return ""
    name = str((info or {}).get("name") or "").strip()
    host = str((info or {}).get("hostapi", ""))
    return f"{name}#{host}" if name else ""


def read_snapshot(sd_module: Any) -> DeviceSnapshot:
    """Current default input/output, tolerating a machine with neither."""
    try:
        default = getattr(sd_module, "default", None)
        device = getattr(default, "device", (None, None))
        input_index, output_index = device[0], device[1]
    except Exception:
        input_index = output_index = None
    inputs = 0
    try:
        inputs = sum(
            1 for item in sd_module.query_devices()
            if int((item or {}).get("max_input_channels", 0) or 0) > 0
        )
    except Exception:
        inputs = 0
    return DeviceSnapshot(
        input_name=_name_of(sd_module, input_index),
        output_name=_name_of(sd_module, output_index),
        input_count=inputs,
    )


def device_label(name: str) -> str:
    """Drop the ``#hostapi`` suffix that keeps snapshots comparable.

    ``"Headset Mic#0"`` distinguishes the same hardware seen through WASAPI and
    MME, which matters for change detection but reads as noise on screen.
    """
    return str(name or "").rsplit("#", 1)[0].strip()


def refresh_device_list(sd_module: Any) -> bool:
    """Rebuild PortAudio's cached device list.

    Required for hardware connected after start-up to become visible at all.
    Failure is not fatal: we simply keep the old list and try again later.
    """
    terminate = getattr(sd_module, "_terminate", None)
    initialize = getattr(sd_module, "_initialize", None)
    if not callable(terminate) or not callable(initialize):
        return False
    try:
        terminate()
        initialize()
        return True
    except Exception:
        logger.debug("オーディオデバイス一覧の再取得に失敗", exc_info=True)
        return False


class AudioDeviceMonitor:
    """Report when the audio hardware in use has changed."""

    def __init__(self, sd_module: Any | None = None) -> None:
        if sd_module is None:
            import sounddevice as sd_module  # noqa: PLC0415
        self._sd = sd_module
        self._last: DeviceSnapshot | None = None

    @property
    def last(self) -> DeviceSnapshot | None:
        return self._last

    def poll(self, *, refresh: bool = False) -> tuple[str, DeviceSnapshot]:
        """Return ``(reason, snapshot)``; an empty reason means no change.

        The very first poll reports ``"initial"`` so a caller can record the
        baseline without treating it as a hardware event.

        ``refresh_device_list()`` calls PortAudio's process-global
        terminate/initialize pair.  Doing that while input/output streams are
        alive stops those streams, so normal heartbeat polling must never
        refresh implicitly.  Callers may request ``refresh=True`` only after
        they have detected a stopped device and can reopen every stream.
        """
        if refresh:
            refresh_device_list(self._sd)
        snapshot = read_snapshot(self._sd)
        reason = snapshot.differs_from(self._last)
        self._last = snapshot
        return reason, snapshot
