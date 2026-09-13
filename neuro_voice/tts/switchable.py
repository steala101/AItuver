"""Thread-safe live switching for TTS backends.

The local pipeline and the Discord bridge share this object.  Replacing its
inner backend therefore updates every output path without rebuilding either
conversation engine.  A retired backend is closed only after in-flight
synthesis calls have returned.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

from neuro_voice.tts.base import TTSBackend

logger = logging.getLogger(__name__)


class SwitchableTTS(TTSBackend):
    """A stable TTS reference whose implementation can be replaced live."""

    def __init__(self, backend: TTSBackend, backend_name: str, volume: float = 1.0):
        self._lock = threading.RLock()
        self._backend = backend
        self._backend_name = str(backend_name)
        self._volume = max(0.0, min(2.0, float(volume)))
        self._active: dict[TTSBackend, int] = {}
        self._retired: set[TTSBackend] = set()
        self._closed = False

    @property
    def backend_name(self) -> str:
        with self._lock:
            return self._backend_name

    @property
    def inner(self) -> TTSBackend:
        with self._lock:
            return self._backend

    def replace(self, backend: TTSBackend, backend_name: str) -> None:
        """Atomically use *backend* for all future synthesis calls."""
        close_now = None
        with self._lock:
            if self._closed:
                self._safe_close(backend)
                raise RuntimeError("TTS is already closed")
            old = self._backend
            self._backend = backend
            self._backend_name = str(backend_name)
            if old is backend:
                return
            if self._active.get(old, 0):
                self._retired.add(old)
            else:
                close_now = old
        if close_now is not None:
            self._safe_close(close_now)

    def _acquire(self) -> TTSBackend:
        with self._lock:
            if self._closed:
                raise RuntimeError("TTS is already closed")
            backend = self._backend
            self._active[backend] = self._active.get(backend, 0) + 1
            return backend

    def _release(self, backend: TTSBackend) -> None:
        close_now = False
        with self._lock:
            remaining = self._active.get(backend, 0) - 1
            if remaining > 0:
                self._active[backend] = remaining
            else:
                self._active.pop(backend, None)
                if backend in self._retired:
                    self._retired.remove(backend)
                    close_now = True
        if close_now:
            self._safe_close(backend)

    @staticmethod
    def _safe_close(backend: TTSBackend) -> None:
        try:
            backend.close()
        except Exception:
            logger.warning("TTSバックエンドの解放に失敗しました", exc_info=True)

    def synthesize(self, text: str, emotion: str | None = None, style=None):
        backend = self._acquire()
        try:
            audio, sample_rate = backend.synthesize(text, emotion=emotion, style=style)
            with self._lock:
                volume = self._volume
            if abs(volume - 1.0) > 1e-4:
                audio = np.clip(np.asarray(audio, dtype=np.float32) * volume, -1.0, 1.0)
            return np.ascontiguousarray(audio, dtype=np.float32), sample_rate
        finally:
            self._release(backend)

    @property
    def volume(self) -> float:
        with self._lock:
            return self._volume

    def set_volume(self, volume: float) -> float:
        with self._lock:
            self._volume = max(0.0, min(2.0, float(volume)))
            return self._volume

    def adjust_volume(self, delta: float) -> float:
        with self._lock:
            self._volume = max(0.0, min(2.0, self._volume + float(delta)))
            return self._volume

    def ensure_ready(self) -> bool:
        backend = self.inner
        method = getattr(backend, "ensure_ready", None)
        return bool(method()) if callable(method) else True

    @property
    def speaker(self) -> Any:
        return getattr(self.inner, "speaker", None)

    def set_speaker(self, speaker: Any) -> None:
        method = getattr(self.inner, "set_speaker", None)
        if not callable(method):
            raise RuntimeError(f"{self.backend_name} は話者切替に対応していません")
        method(speaker)

    def list_speakers(self) -> list[dict]:
        method = getattr(self.inner, "list_speakers", None)
        if not callable(method):
            raise RuntimeError(f"{self.backend_name} は話者一覧に対応していません")
        return method()

    def set_pronunciations(self, pronunciations: dict | None) -> None:
        method = getattr(self.inner, "set_pronunciations", None)
        if callable(method):
            method(pronunciations)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            backends = {self._backend, *self._retired}
            self._retired.clear()
        for backend in backends:
            self._safe_close(backend)
