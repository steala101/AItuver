"""スピーカー再生。専用スレッドでキューを消費し、割り込み(clear)に対応する。"""
from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections import deque
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

_BLOCK = 1024  # フェードを滑らかにするため小さめのブロックで再生する


class SpeakerPlayback:
    """TTS音声を順次再生する。"""

    def __init__(self, device: int | str | None = None, prebuffer_s: float = 0.0):
        self._device = device
        self._prebuffer_s = max(0.0, float(prebuffer_s))
        self._q: queue.Queue[tuple[np.ndarray, int, Callable | None, Callable | None, Callable[[], bool] | None]] = queue.Queue()
        self._abort = threading.Event()
        self._stop = threading.Event()
        self._active = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._running = False
        self._gain_lock = threading.Lock()
        self._gain_start = 1.0
        self._gain_target = 1.0
        self._gain_changed_at = time.monotonic()
        self._gain_duration_s = 0.0
        self._abort_after_fade = False
        # Barge-in is deliberately independent from ``clear``.  A pause keeps
        # both the current PCM cursor and later queue items intact until STT
        # tells us whether the user actually interrupted the assistant.
        self._paused = threading.Event()
        # Set when the output hardware changed and the stream must be rebuilt.
        self._reopen = threading.Event()
        #: Why the last stream open failed, for the status line.
        self.last_error = ""
        self.muted = False  # True の間はローカルスピーカーから音を出さない

    def set_muted(self, muted: bool) -> None:
        """スピーカーのミュート切替。ミュート時は再生中の音声も止める。"""
        self.muted = bool(muted)
        if self.muted:
            self.clear()

    def start(self) -> None:
        with self._thread_lock:
            self._running = True
            self._stop.clear()
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="playback", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._thread_lock:
            self._running = False
            self._stop.set()
            thread = self._thread
        self.clear()
        if thread is not None:
            thread.join(timeout=2)
        with self._thread_lock:
            self._thread = None

    def play(
        self, audio: np.ndarray, sample_rate: int, on_start: Callable | None = None,
        on_complete: Callable[[int, int, bool], None] | None = None,
        should_play: Callable[[], bool] | None = None,
    ) -> None:
        """再生キューに追加する。on_start は最初のブロック再生直前に呼ばれる。"""
        if self.muted:
            # ミュート中は音を出さないが、レイテンシ計測用コールバックだけは呼ぶ
            if on_start is not None:
                try:
                    on_start()
                except Exception:
                    logger.exception("on_start コールバックでエラー")
            return
        self._q.put((np.asarray(audio, dtype=np.float32).reshape(-1), sample_rate, on_start, on_complete, should_play))
        # An unexpected backend exception must not turn all future TTS into a
        # black hole.  The supervised worker normally survives on its own,
        # but this also recovers if it died between queue operations.
        with self._thread_lock:
            should_restart = self._running and (
                self._thread is None or not self._thread.is_alive()
            )
        if should_restart:
            self.start()

    def clear(self) -> None:
        """キューを破棄し、再生中の音声を即時停止する。"""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if self._active.is_set():
            self._abort.set()

    def pause_immediately(self) -> None:
        """Silence output without advancing or discarding queued PCM.

        The playback worker observes this before each audio block (1024
        frames), so normal output devices become silent well below 100 ms.
        """
        self._paused.set()
        self._set_gain(0.0, 0.0)

    def resume_from_pause(self, fade_ms: float = 80) -> None:
        """Continue the current PCM cursor after an acknowledgement."""
        if not self._paused.is_set():
            return
        self._paused.clear()
        self._set_gain(1.0, max(0.0, min(float(fade_ms), 120.0)))

    def discard_paused_audio(self) -> None:
        """Discard current and queued audio after a confirmed barge-in."""
        self._paused.clear()
        self.clear()
        # ``pause_immediately`` sets the output gain to zero.  A confirmed
        # interruption discards only the old response; it must never leave
        # the next VOICEVOX response permanently muted.
        self.restore_volume(0)

    def duck(self, gain: float = 0.18, fade_ms: float = 250) -> None:
        """ユーザー発話を検出したら、AI音声を一時的に小さくする。"""
        self._set_gain(gain, fade_ms)

    def restore_volume(self, fade_ms: float = 180) -> None:
        """相槌・誤検知だった時に、AI音声の音量を自然に戻す。"""
        with self._gain_lock:
            self._abort_after_fade = False
        self._set_gain(1.0, fade_ms)

    def fade_out_and_clear(self, fade_ms: float = 280) -> None:
        """今の文だけを短くフェードアウトしてから止め、待機中の文も破棄する。"""
        has_current_audio = self._active.is_set()
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if not has_current_audio:
            # 再生中のブロックがないなら、次の応答を無音にしないよう即座に戻す。
            self.restore_volume(0)
            return
        with self._gain_lock:
            if self._abort_after_fade:
                return
            self._abort_after_fade = True
        self._set_gain(0.0, fade_ms)

    def _set_gain(self, target: float, fade_ms: float) -> None:
        now = time.monotonic()
        with self._gain_lock:
            self._gain_start = self._gain_at(now)
            self._gain_target = max(0.0, min(1.0, float(target)))
            self._gain_changed_at = now
            self._gain_duration_s = max(0.0, float(fade_ms) / 1000)

    def _gain_at(self, now: float) -> float:
        if self._gain_duration_s <= 0:
            return self._gain_target
        t = min(1.0, max(0.0, (now - self._gain_changed_at) / self._gain_duration_s))
        return self._gain_start + (self._gain_target - self._gain_start) * t

    def _gain_envelope(self, frames: int, sample_rate: int) -> tuple[float, float, bool]:
        now = time.monotonic()
        with self._gain_lock:
            start = self._gain_at(now)
            end = self._gain_at(now + frames / sample_rate)
            should_abort = self._abort_after_fade and end <= 0.001
            if should_abort:
                self._abort_after_fade = False
                # 次の応答は通常音量で始める。現在のブロックは end=0 まで再生する。
                self._gain_start = self._gain_target = 1.0
                self._gain_changed_at = now + frames / sample_rate
                self._gain_duration_s = 0.0
        return start, end, should_abort

    @property
    def is_active(self) -> bool:
        return self._active.is_set() or not self._q.empty()

    def request_reopen(self) -> None:
        """Rebuild the output stream before the next chunk.

        Called when the default output device changes, so audio follows the
        headset instead of continuing into a device nobody is wearing.
        """
        self._reopen.set()

    @property
    def pending_seconds(self) -> float:
        """Audio already queued but not yet played, in seconds.

        Continuation segments use this as backpressure so a long directive
        never synthesizes minutes of speech ahead of what is being heard.
        """
        total = 0.0
        with contextlib.suppress(Exception):
            for item in list(self._q.queue):
                audio, sample_rate = item[0], item[1]
                total += len(audio) / max(1, int(sample_rate))
        return round(total, 3)

    @property
    def paused(self) -> bool:
        """Barge-in保留で無音化中かどうか (VADループの自動復帰判定用)。"""
        return self._paused.is_set()

    @staticmethod
    def _close_stream(stream: sd.OutputStream | None) -> None:
        if stream is None:
            return
        with contextlib.suppress(Exception):
            stream.stop()
        with contextlib.suppress(Exception):
            stream.close()

    def _open_stream(self, sample_rate: int) -> sd.OutputStream:
        stream = sd.OutputStream(
            samplerate=sample_rate, channels=1, dtype="float32",
            device=self._device,
        )
        stream.start()
        return stream

    def _run(self) -> None:
        """Supervise the queue consumer until an explicit stop.

        PortAudio can invalidate a stream when a USB headset changes state.
        A single backend exception must not permanently kill local speech.
        """
        while not self._stop.is_set():
            try:
                self._consume()
            except Exception as exc:
                self.last_error = str(exc)[:160]
                self._active.clear()
                self._paused.clear()
                self.restore_volume(0)
                logger.exception(
                    "再生ワーカーで予期しないエラー。ストリームを再構築して継続します"
                )
                if not self._stop.is_set():
                    time.sleep(0.1)

    def _consume(self) -> None:
        stream: sd.OutputStream | None = None
        current_sr = 0
        primed: deque[tuple[np.ndarray, int, Callable | None, Callable | None, Callable[[], bool] | None]] = deque()
        try:
            while not self._stop.is_set():
                try:
                    if primed:
                        audio, sr, on_start, on_complete, should_play = primed.popleft()
                    else:
                        audio, sr, on_start, on_complete, should_play = self._q.get(timeout=0.1)
                except queue.Empty:
                    self._active.clear()
                    continue
                # Do a short opening prebuffer only while idle.  TTS chunks
                # stay small for interruption accuracy, but starting from a
                # 100--300 ms fragment makes a slower VOICEVOX synthesis
                # sound like a broken/cut-off voice.
                if (not self._active.is_set() and self._prebuffer_s > 0
                        and len(audio) / max(sr, 1) < self._prebuffer_s):
                    buffered_s = len(audio) / max(sr, 1)
                    staged = [(audio, sr, on_start, on_complete, should_play)]
                    deadline = time.monotonic() + 0.18
                    while buffered_s < self._prebuffer_s:
                        try:
                            nxt = self._q.get(timeout=max(0.0, deadline - time.monotonic()))
                        except queue.Empty:
                            break
                        staged.append(nxt)
                        buffered_s += len(nxt[0]) / max(nxt[1], 1)
                    audio, sr, on_start, on_complete, should_play = staged[0]
                    primed.extend(staged[1:])
                if should_play is not None and not should_play():
                    if on_complete is not None:
                        try:
                            on_complete(0, len(audio), False)
                        except Exception:
                            logger.exception("on_complete callback error")
                    continue
                if stream is None or sr != current_sr or self._reopen.is_set():
                    self._reopen.clear()
                    if stream is not None:
                        self._close_stream(stream)
                        stream = None
                    try:
                        stream = self._open_stream(sr)
                    except Exception as exc:
                        # A missing or busy output device must not kill this
                        # thread: the speaker may be switched on at any moment,
                        # and a dead thread never recovers.
                        stream = None
                        current_sr = 0
                        self._active.clear()
                        self.last_error = str(exc)[:160]
                        logger.warning(
                            "再生デバイスを開けません (%s)。この音声は破棄して待機します",
                            self.last_error,
                        )
                        if on_complete is not None:
                            with contextlib.suppress(Exception):
                                on_complete(0, len(audio), False)
                        time.sleep(0.5)
                        continue
                    self.last_error = ""
                    current_sr = sr
                    logger.info("再生ストリーム開始 (sr=%d)", sr)
                logger.debug("再生開始 (%.1f秒分)", len(audio) / sr)
                self._abort.clear()
                self._active.set()
                if on_start is not None:
                    try:
                        on_start()
                    except Exception:
                        logger.exception("on_start コールバックでエラー")
                played_frames = 0
                write_failed = False
                for i in range(0, len(audio), _BLOCK):
                    # Continue feeding explicit silence while paused.  This
                    # avoids a stale hardware buffer and, importantly, does
                    # not advance ``played_frames``.
                    while self._paused.is_set() and not self._abort.is_set() and not self._stop.is_set():
                        silence = np.zeros(
                            min(_BLOCK, len(audio) - i), dtype=np.float32,
                        )
                        stream, current_sr, ok = self._write_with_recovery(
                            stream, silence, sr,
                        )
                        if not ok:
                            write_failed = True
                            break
                    if write_failed:
                        break
                    if self._abort.is_set() or self._stop.is_set():
                        break
                    block = audio[i : i + _BLOCK]
                    start_gain, end_gain, should_abort = self._gain_envelope(len(block), sr)
                    if abs(start_gain - end_gain) < 0.001:
                        block = block * start_gain
                    else:
                        block = block * np.linspace(
                            start_gain, end_gain, len(block), dtype=np.float32,
                        )
                    stream, current_sr, ok = self._write_with_recovery(
                        stream, block, sr,
                    )
                    if not ok:
                        write_failed = True
                        break
                    played_frames += len(block)
                    if should_abort:
                        self._abort.set()
                if on_complete is not None:
                    try:
                        on_complete(played_frames, len(audio), played_frames >= len(audio))
                    except Exception:
                        logger.exception("on_complete callback error")
                if write_failed:
                    # A failed chunk is finalized as partial so the response
                    # tracker and turn manager do not remain "speaking".
                    self._active.clear()
                    self._paused.clear()
                    self.restore_volume(0)
        finally:
            self._active.clear()
            self._close_stream(stream)

    def _write_with_recovery(
        self,
        stream: sd.OutputStream | None,
        block: np.ndarray,
        sample_rate: int,
        *,
        max_attempts: int = 3,
    ) -> tuple[sd.OutputStream | None, int, bool]:
        """Write one PCM block, reopening a stopped PortAudio stream in-place.

        The same block is retried, so the played-frame cursor and callback
        identity stay intact.  This avoids both missing text and duplicate
        tracker completion events.
        """
        current = stream
        for attempt in range(1, max_attempts + 1):
            if current is None:
                try:
                    current = self._open_stream(sample_rate)
                    logger.info(
                        "停止した再生ストリームを復旧しました (sr=%d, attempt=%d)",
                        sample_rate, attempt,
                    )
                except Exception as exc:
                    self.last_error = str(exc)[:160]
                    if attempt < max_attempts:
                        time.sleep(0.05)
                    continue
            try:
                current.write(block)
                self.last_error = ""
                return current, sample_rate, True
            except Exception as exc:
                self.last_error = str(exc)[:160]
                logger.warning(
                    "再生ストリームが停止しました。再構築します "
                    "(attempt=%d/%d, error=%s)",
                    attempt, max_attempts, self.last_error,
                )
                self._close_stream(current)
                current = None
                if attempt < max_attempts:
                    time.sleep(0.05)
        logger.error(
            "再生ストリームを復旧できませんでした。この音声チャンクを中止します: %s",
            self.last_error or "unknown error",
        )
        return None, 0, False
