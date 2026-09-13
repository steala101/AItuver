"""VOICEVOX エンジン (HTTP API) による高速TTSバックエンド。

VOICEVOX アプリ (またはエンジン単体) を起動しておくこと。
既定では http://127.0.0.1:50021 で待ち受けている。
"""
from __future__ import annotations

import io
import logging
import time

import numpy as np
import requests
import soundfile as sf

from neuro_voice.tts.base import TTSBackend
from neuro_voice.tts.style import SpeechStyle, StyleManager
from neuro_voice.tts.pronunciation import apply_pronunciations, normalize_pronunciations

logger = logging.getLogger(__name__)


class VoicevoxTTS(TTSBackend):
    """VOICEVOX による音声合成 (1文あたり数百ms)。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:50021",
        speaker: int = 3,
        speed: float = 1.0,
        intonation: float = 1.0,
        volume: float = 1.0,
        pitch: float = 0.0,
        timeout: float = 30.0,
        auto_launch: bool = True,
        engine_path: str | None = None,
        pronunciations: dict | None = None,
        on_status=None,
    ):
        self._base = base_url.rstrip("/")
        self._speaker = int(speaker)
        self._speed = float(speed)
        self._intonation = float(intonation)
        self._volume = float(volume)
        self._pitch = float(pitch)
        self._timeout = timeout
        self._auto_launch = bool(auto_launch)
        self._engine_path = engine_path
        self._on_status = on_status
        self._last_relaunch = 0.0
        self._session = requests.Session()
        self._style_manager = StyleManager()
        self._pronunciations = normalize_pronunciations(pronunciations)
        try:
            version = self._session.get(f"{self._base}/version", timeout=3).text.strip()
            logger.info("VOICEVOX %s に接続 (speaker=%d)", version, self._speaker)
        except requests.ConnectionError:
            logger.warning(
                "VOICEVOX エンジンに接続できません (%s)。"
                "VOICEVOX アプリを起動してください。", self._base,
            )

    def _try_relaunch(self) -> bool:
        """接続できないときにエンジンの (再) 起動を試みる。連発防止のクールダウン付き。"""
        if not self._auto_launch:
            return False
        now = time.monotonic()
        if now - self._last_relaunch < 20.0:
            return False
        self._last_relaunch = now
        logger.warning("VOICEVOX に接続できないため、エンジンの再起動を試みます")
        if self._on_status is not None:
            try:
                self._on_status("VOICEVOX エンジンに接続できません。再起動しています...")
            except Exception:
                pass
        from neuro_voice.tts.voicevox_launcher import ensure_engine

        return ensure_engine(
            base_url=self._base,
            engine_path=self._engine_path,
            wait_s=45.0,
            on_status=self._on_status,
        )

    def ensure_ready(self) -> bool:
        """Check/start the engine before a latency-sensitive first utterance."""
        try:
            response = self._session.get(f"{self._base}/version", timeout=2)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return self._try_relaunch()

    @property
    def speaker(self) -> int:
        return self._speaker

    def set_speaker(self, speaker_id: int) -> None:
        """話者 (スタイルID) を切り替える。次の合成から反映される。"""
        self._speaker = int(speaker_id)
        logger.info("話者を変更: speaker=%d", self._speaker)

    def set_pronunciations(self, pronunciations: dict | None) -> None:
        self._pronunciations = normalize_pronunciations(pronunciations)

    def close(self) -> None:
        self._session.close()

    def list_speakers(self) -> list[dict]:
        """利用可能な話者スタイルの一覧を返す。"""
        try:
            r = self._session.get(f"{self._base}/speakers", timeout=5)
        except requests.ConnectionError:
            if not self._try_relaunch():
                raise
            r = self._session.get(f"{self._base}/speakers", timeout=5)
        r.raise_for_status()
        result: list[dict] = []
        for sp in r.json():
            for style in sp.get("styles", []):
                result.append(
                    {"id": style["id"], "label": f"{sp['name']}({style['name']})"}
                )
        return result

    def synthesize(
        self, text: str, emotion: str | None = None, style: SpeechStyle | None = None,
    ) -> tuple[np.ndarray, int]:
        start = time.perf_counter()
        spoken_text = apply_pronunciations(text, self._pronunciations)
        try:
            query = self._session.post(
                f"{self._base}/audio_query",
                params={"text": spoken_text, "speaker": self._speaker},
                timeout=self._timeout,
            )
        except requests.ConnectionError:
            # エンジンが落ちている → 再起動を試みて1回だけリトライ (自己修復)
            if not self._try_relaunch():
                raise
            query = self._session.post(
                f"{self._base}/audio_query",
                params={"text": spoken_text, "speaker": self._speaker},
                timeout=self._timeout,
            )
        query.raise_for_status()
        payload = query.json()
        style = style or self._style_manager.resolve(emotion, None)
        payload["speedScale"] = max(0.5, min(2.0, self._speed * style.speed))
        payload["intonationScale"] = max(0.0, min(2.0, self._intonation * style.intonation))
        payload["volumeScale"] = max(0.0, min(2.0, self._volume * style.volume))
        payload["pitchScale"] = max(-0.15, min(0.15, self._pitch + style.pitch))
        payload["prePhonemeLength"] = style.pre_phoneme
        payload["postPhonemeLength"] = style.post_phoneme

        synthesis = self._session.post(
            f"{self._base}/synthesis",
            params={"speaker": self._speaker},
            json=payload,
            timeout=self._timeout,
        )
        synthesis.raise_for_status()
        wav, sr = sf.read(io.BytesIO(synthesis.content), dtype="float32")
        if wav.ndim > 1:
            wav = wav[:, 0]
        logger.info(
            "TTS合成 %.2fs (%d文字, 音声%.1f秒): %s",
            time.perf_counter() - start, len(text), len(wav) / sr, text[:20],
        )
        return np.ascontiguousarray(wav, dtype=np.float32), int(sr)
