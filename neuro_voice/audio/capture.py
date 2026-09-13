"""マイク入力。sounddevice コールバックから asyncio Queue へフレームを渡す。"""
from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class MicCapture:
    """16kHz mono float32 のフレームを Queue に供給する。"""

    def __init__(
        self,
        out_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        sample_rate: int = 16000,
        frame_samples: int = 512,
        device: int | str | None = None,
    ):
        self._q = out_queue
        self._loop = loop
        self._sr = sample_rate
        self._frame = frame_samples
        self._device = device
        self._stream: sd.InputStream | None = None
        self.muted = False
        #: Why the last open failed, for the status line.  Empty when fine.
        self.last_error = ""
        #: Which device the open actually used, and whether it was the one
        #: configured.  The status line has to be able to say "the microphone
        #: you chose was not there, so the default one is in use".
        self.opened_device: int | str | None = None
        self.used_fallback = False

    def _open(self, device: int | str | None) -> bool:
        try:
            self._stream = sd.InputStream(
                samplerate=self._sr,
                blocksize=self._frame,
                channels=1,
                dtype="float32",
                device=device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self.last_error = str(exc)[:160]
            return False
        return True

    def start(self, *, allow_fallback: bool = False) -> bool:
        """Open the microphone.  Returns False when no device is available.

        A headset that is switched on later is a normal situation, not a fatal
        error: failing to start here must leave the application running so the
        device watcher can pick the microphone up when it appears.

        ``allow_fallback`` is for the explicit "re-detect" action only.  The
        configured device is always tried first; falling back to the system
        default silently would be worse than failing, so automatic paths never
        do it — the person has to have asked.
        """
        if self._stream is not None:
            return True
        if self._open(self._device):
            self.last_error = ""
            self.opened_device = self._device
            self.used_fallback = False
            logger.info("マイク入力開始 (sr=%d, frame=%d)", self._sr, self._frame)
            return True
        configured_error = self.last_error
        if allow_fallback and self._device is not None and self._open(None):
            self.last_error = ""
            self.opened_device = None
            self.used_fallback = True
            logger.warning(
                "指定マイク(%s)を開けなかったため既定デバイスで開きました (%s)",
                self._device, configured_error,
            )
            return True
        self.last_error = configured_error
        logger.warning("マイクを開けませんでした (%s)。接続されたら自動で開き直します", self.last_error)
        return False

    @property
    def active(self) -> bool:
        stream = self._stream
        if stream is None:
            return False
        try:
            return bool(stream.active)
        except Exception as exc:
            self.last_error = str(exc)[:160]
            return False

    def restart(self, *, allow_fallback: bool = False) -> bool:
        """Reopen after a hardware change.

        See :meth:`start` for why ``allow_fallback`` is opt-in.
        """
        self.stop()
        return self.start(allow_fallback=allow_fallback)

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:
            logger.debug("マイク停止でエラー (無視)", exc_info=True)
        logger.info("マイク入力停止")

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            logger.debug("mic status: %s", status)
        if self.muted:
            return
        frame = indata[:, 0].copy()

        def _put(f: np.ndarray = frame) -> None:
            if not self._q.full():
                self._q.put_nowait(f)

        try:
            self._loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # ループ終了後のコールバックは無視
