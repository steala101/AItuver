"""Discord 音声の変換とユーザーごとの発話区切り。

Discord の音声は 48kHz ステレオ 16bit PCM (20ms フレーム)。
STT/声処理は 16kHz モノラル float32 なので相互変換する。
"""
from __future__ import annotations

import time

import numpy as np

DISCORD_SR = 48000
PIPE_SR = 16000


def discord_to_16k(pcm: bytes) -> np.ndarray:
    """48kHz ステレオ int16 バイト列 → 16kHz モノラル float32。"""
    a = np.frombuffer(pcm, dtype=np.int16)
    if len(a) == 0:
        return np.zeros(0, dtype=np.float32)
    if len(a) % 2:
        a = a[:-1]
    mono = a.reshape(-1, 2).mean(axis=1).astype(np.float32) / 32768.0
    # 48k → 16k: 3サンプル平均 (簡易ローパス込みのデシメーション)
    n = len(mono) - (len(mono) % 3)
    return mono[:n].reshape(-1, 3).mean(axis=1).astype(np.float32)


def to_discord_pcm(wav: np.ndarray, sr: int) -> bytes:
    """任意サンプルレートのモノラル float32 → 48kHz ステレオ int16 バイト列。"""
    wav = np.asarray(wav, dtype=np.float32)
    if len(wav) == 0:
        return b""
    if sr != DISCORD_SR:
        # 線形補間リサンプル (TTS音声には十分な品質)
        n_out = int(round(len(wav) * DISCORD_SR / sr))
        x_out = np.linspace(0.0, len(wav) - 1, n_out, dtype=np.float64)
        wav = np.interp(x_out, np.arange(len(wav), dtype=np.float64), wav).astype(np.float32)
    wav = np.clip(wav, -1.0, 1.0)
    s16 = (wav * 32767).astype(np.int16)
    stereo = np.repeat(s16[:, None], 2, axis=1)  # L/R 複製
    return stereo.tobytes()


class UtteranceSegmenter:
    """ユーザー1人分の 16kHz ストリームを発話単位に区切る (エネルギーVAD)。

    Silero をユーザー数ぶん複製すると重いので、ノイズフロア追従の
    エネルギーしきい値で区切る。Discord は各ユーザーの音声が分離済み
    かつノイズ抑制済みなので、これで十分実用になる。
    """

    def __init__(
        self,
        sample_rate: int = PIPE_SR,
        frame_ms: float = 20.0,
        start_ms: float = 120.0,
        end_silence_ms: float = 600.0,
        min_speech_ms: float = 350.0,
        max_speech_s: float = 20.0,
        pre_roll_ms: float = 200.0,
    ):
        self._sr = sample_rate
        self._frame = int(sample_rate * frame_ms / 1000)
        self._start_frames = max(1, int(start_ms / frame_ms))
        self._end_frames = max(1, int(end_silence_ms / frame_ms))
        self._min_samples = int(sample_rate * min_speech_ms / 1000)
        self._max_samples = int(sample_rate * max_speech_s)
        self._pre_roll = max(1, int(pre_roll_ms / frame_ms))
        self._buf = np.zeros(0, dtype=np.float32)
        self._noise = 0.003  # ノイズフロア (EMAで追従)
        self._speaking = False
        self._voiced = 0
        self._silent = 0
        self._utter: list[np.ndarray] = []
        self._recent: list[np.ndarray] = []
        self._pre_frames = 0
        self.last_activity = time.monotonic()

    def _is_voice(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame**2) + 1e-12))
        thr = max(self._noise * 3.0, 0.006)
        if rms < thr:
            # 無音のときだけノイズフロアを更新
            self._noise = 0.95 * self._noise + 0.05 * rms
        return rms >= thr

    def feed(self, chunk: np.ndarray) -> np.ndarray | None:
        """音声チャンクを追加し、発話が完結したらその音声を返す。"""
        if len(chunk):
            self._buf = np.concatenate([self._buf, chunk])
        done = None
        while len(self._buf) >= self._frame:
            frame = self._buf[: self._frame]
            self._buf = self._buf[self._frame :]
            out = self._feed_frame(frame)
            if out is not None:
                done = out
        return done

    def _feed_frame(self, frame: np.ndarray) -> np.ndarray | None:
        voiced = self._is_voice(frame)
        if not self._speaking:
            self._recent.append(frame)
            if len(self._recent) > self._pre_roll:
                self._recent.pop(0)
            self._voiced = self._voiced + 1 if voiced else 0
            if self._voiced >= self._start_frames:
                self._speaking = True
                self._silent = 0
                self._utter = list(self._recent)
                self._pre_frames = len(self._utter)  # プリロールは発話長に数えない
                self.last_activity = time.monotonic()
            return None

        self._utter.append(frame)
        self.last_activity = time.monotonic()
        self._silent = self._silent + 1 if not voiced else 0
        total = sum(len(f) for f in self._utter)
        if self._silent >= self._end_frames or total >= self._max_samples:
            self._speaking = False
            self._voiced = 0
            self._recent.clear()
            audio = np.concatenate(self._utter)
            # プリロールと末尾の無音は実発話長に数えない
            speech_frames = len(self._utter) - self._pre_frames - self._silent
            self._utter = []
            if speech_frames * self._frame >= self._min_samples:
                return audio
        return None

    @property
    def speaking(self) -> bool:
        return self._speaking

    def current_audio(self, *, min_speech_ms: float = 0.0) -> np.ndarray | None:
        """Return a copy of the in-progress utterance for display-only STT."""
        if not self._speaking or not self._utter:
            return None
        audio = np.concatenate(self._utter)
        speech_samples = max(0, len(audio) - self._pre_frames * self._frame)
        if speech_samples < int(self._sr * max(0.0, min_speech_ms) / 1000):
            return None
        return audio

    def flush(self) -> np.ndarray | None:
        """パケット途絶・通話終了時に、話し途中の音声を強制確定して取り出す。"""
        if self._speaking and self._utter:
            audio = np.concatenate(self._utter)
            speech_frames = len(self._utter) - self._pre_frames
            self._speaking = False
            self._voiced = 0
            self._utter = []
            self._recent.clear()
            if speech_frames * self._frame >= self._min_samples:
                return audio
        return None
