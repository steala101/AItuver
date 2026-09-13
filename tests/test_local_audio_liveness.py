"""Regression tests for PortAudio stream liveness.

The device watcher used to terminate/reinitialize PortAudio every few seconds.
That leaves existing sounddevice streams as objects whose backend stream is
already stopped.  Local microphone capture and TTS must detect and recover
from that state instead of staying silently dead.
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

import numpy as np

from neuro_voice.audio.capture import MicCapture
from neuro_voice.audio.devices import AudioDeviceMonitor
from neuro_voice.audio.playback import SpeakerPlayback


class _FakeOutputBackend:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.created = 0

    def open(self, **_kwargs):
        self.created += 1
        backend = self

        class Stream:
            active = False

            def start(self):
                self.active = True

            def write(self, _block):
                if backend.failures > 0:
                    backend.failures -= 1
                    self.active = False
                    raise RuntimeError("Stream is stopped")

            def stop(self):
                self.active = False

            def close(self):
                self.active = False

        return Stream()


class LocalAudioLivenessTests(unittest.TestCase):
    def test_normal_device_poll_never_reinitializes_portaudio(self):
        class Default:
            device = (0, 1)

        class SoundDevice:
            default = Default()

            def __init__(self):
                self.refreshes = 0
                self.devices = [
                    {
                        "name": "Mic", "hostapi": 0,
                        "max_input_channels": 1,
                    },
                    {
                        "name": "Speaker", "hostapi": 0,
                        "max_output_channels": 2,
                    },
                ]

            def query_devices(self, index=None):
                return self.devices if index is None else self.devices[index]

            def _terminate(self):
                self.refreshes += 1

            def _initialize(self):
                pass

        backend = SoundDevice()
        monitor = AudioDeviceMonitor(backend)
        monitor.poll()
        monitor.poll()
        self.assertEqual(backend.refreshes, 0)
        monitor.poll(refresh=True)
        self.assertEqual(backend.refreshes, 1)

    def test_playback_reopens_stopped_stream_without_losing_chunk(self):
        backend = _FakeOutputBackend(failures=1)
        playback = SpeakerPlayback()
        completed = []
        done = threading.Event()

        def on_complete(played, total, ok):
            completed.append((played, total, ok))
            done.set()

        with patch(
            "neuro_voice.audio.playback.sd.OutputStream",
            side_effect=backend.open,
        ):
            playback.start()
            playback.play(
                np.ones(2048, dtype=np.float32), 16000,
                on_complete=on_complete,
            )
            self.assertTrue(done.wait(2.0))
            self.assertTrue(playback._thread is not None)
            self.assertTrue(playback._thread.is_alive())
            playback.stop()

        self.assertGreaterEqual(backend.created, 2)
        self.assertEqual(completed, [(2048, 2048, True)])

    def test_failed_chunk_does_not_kill_future_playback(self):
        backend = _FakeOutputBackend(failures=10)
        playback = SpeakerPlayback()
        first_done = threading.Event()
        second_done = threading.Event()
        results = []

        with patch(
            "neuro_voice.audio.playback.sd.OutputStream",
            side_effect=backend.open,
        ):
            playback.start()
            playback.play(
                np.ones(1024, dtype=np.float32), 16000,
                on_complete=lambda p, t, ok: (
                    results.append(("first", p, t, ok)), first_done.set()
                ),
            )
            self.assertTrue(first_done.wait(2.0))
            backend.failures = 0
            playback.play(
                np.ones(1024, dtype=np.float32), 16000,
                on_complete=lambda p, t, ok: (
                    results.append(("second", p, t, ok)), second_done.set()
                ),
            )
            self.assertTrue(second_done.wait(2.0))
            playback.stop()

        self.assertEqual(results[0], ("first", 0, 1024, False))
        self.assertEqual(results[1], ("second", 1024, 1024, True))

    def test_microphone_active_reflects_backend_stream_state(self):
        event_loop = asyncio.new_event_loop()

        class Stream:
            active = False

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def close(self):
                self.active = False

        stream = Stream()
        capture = MicCapture(asyncio.Queue(), event_loop)
        try:
            with patch(
                "neuro_voice.audio.capture.sd.InputStream",
                return_value=stream,
            ):
                self.assertTrue(capture.start())
                self.assertTrue(capture.active)
                stream.active = False
                self.assertFalse(capture.active)
        finally:
            capture.stop()
            event_loop.close()


if __name__ == "__main__":
    unittest.main()
