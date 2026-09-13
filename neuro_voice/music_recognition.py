"""Explicit, short-clip music identification via an audio-fingerprint service.

This is deliberately separate from the music-command parser: recognition is
only sent to a third party after the user explicitly asks what a song is.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np


@dataclass(frozen=True)
class MusicRecognitionRequest:
    requested: bool
    play_after: bool = False


@dataclass(frozen=True)
class MusicRecognitionResult:
    title: str
    artist: str
    album: str = ""
    song_link: str = ""

    @property
    def query(self) -> str:
        return " - ".join(part for part in (self.artist, self.title) if part).strip()


@dataclass(frozen=True)
class HummingRecognitionRequest:
    requested: bool
    play_after: bool = False


_IDENTIFY_WORDS = (
    "何の曲", "なんの曲", "曲名教", "曲名を教", "曲名わか", "曲名分か",
    "曲を特定", "曲特定", "曲当て", "曲を当て", "曲調べ", "曲を調べ",
    "音楽を特定", "音楽調べ", "曲を認識",
)
_PLAY_WORDS = ("流して", "再生して", "かけて", "流してほしい", "再生してほしい")
_HUMMING_WORDS = ("鼻歌", "はなうた", "口笛", "くちぶえ", "ハミング", "humming")


def parse_music_recognition_request(text: str) -> MusicRecognitionRequest:
    """Recognize an explicit request without hijacking ordinary music chat."""
    normalized = re.sub(r"\s+", "", (text or "").lower())
    requested = any(word in normalized for word in _IDENTIFY_WORDS)
    # Spoken STT commonly produces "この曲なに" rather than "何の曲".
    if not requested and "曲" in normalized and ("なに" in normalized or "何" in normalized):
        requested = any(marker in normalized for marker in ("この", "これ", "いま", "今", "流れて", "さっき"))
    return MusicRecognitionRequest(
        requested=requested,
        play_after=requested and any(word in normalized for word in _PLAY_WORDS),
    )


def parse_humming_recognition_request(text: str) -> HummingRecognitionRequest:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    has_humming = any(word in normalized for word in _HUMMING_WORDS)
    has_search = any(word in normalized for word in ("探して", "調べて", "当てて", "特定", "何の曲", "なんの曲", "曲名"))
    requested = has_humming and has_search
    return HummingRecognitionRequest(
        requested=requested,
        play_after=requested and any(word in normalized for word in _PLAY_WORDS),
    )


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    data = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    return out.getvalue()


def capture_output_audio(
    seconds: float = 8.0,
    sample_rate: int = 16000,
    loopback_device: str | None = None,
) -> np.ndarray:
    """Capture the sound currently playing on a Windows output endpoint.

    Local song identification must fingerprint YouTube/system playback, not
    the user's microphone. An empty device selects the Windows default output;
    an explicit value is resolved by exact endpoint ID (legacy exact display
    names remain accepted for existing configurations).
    """
    try:
        import soundcard as sc
    except Exception as exc:
        raise RuntimeError("再生音の取得には soundcard が必要です") from exc

    from neuro_voice.audio.loopback import (
        _endpoint_id, _find_loopback_microphone,
    )

    requested = str(loopback_device or "").strip()
    speakers = list(sc.all_speakers())
    if requested:
        speaker = next(
            (item for item in speakers if _endpoint_id(item) == requested), None,
        )
        if speaker is None:
            speaker = next(
                (item for item in speakers if str(getattr(item, "name", "")) == requested),
                None,
            )
    else:
        speaker = sc.default_speaker()
    if speaker is None:
        raise RuntimeError("曲名認識に使うWindows出力デバイスが見つかりません")

    microphones = list(sc.all_microphones(include_loopback=True))
    microphone = _find_loopback_microphone(speaker, microphones)
    if microphone is None:
        raise RuntimeError(
            "選択した出力デバイスはループバック取得できません: "
            f"{getattr(speaker, 'name', requested or '既定の出力')}"
        )

    target = max(1, round(max(1.0, float(seconds)) * int(sample_rate)))
    chunks: list[np.ndarray] = []
    captured = 0
    block = min(4096, target)
    with microphone.recorder(samplerate=int(sample_rate), blocksize=max(1024, block)) as recorder:
        while captured < target:
            data = np.asarray(
                recorder.record(numframes=min(block, target - captured)), dtype=np.float32,
            )
            # soundcard/WASAPI can corrupt one-channel recording. Always ask
            # for the endpoint's normal channel layout and downmix ourselves.
            mono = data.mean(axis=1) if data.ndim > 1 else data.reshape(-1)
            mono = np.ascontiguousarray(mono, dtype=np.float32)
            if mono.size == 0:
                break
            chunks.append(mono)
            captured += int(mono.size)
    return np.concatenate(chunks)[:target] if chunks else np.empty(0, dtype=np.float32)


class AudDRecognizer:
    """Small stdlib-only AudD client so enabling P0 adds no new dependency."""

    def __init__(self, *, token_env: str = "AUDD_API_TOKEN", endpoint: str = "https://api.audd.io/"):
        self._token_env = token_env
        self._endpoint = endpoint
        self.last_diagnostic: dict = {}

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self._token_env, "").strip())

    def recognize(self, audio: np.ndarray, sample_rate: int, *, timeout_s: float = 12.0) -> MusicRecognitionResult | None:
        token = os.environ.get(self._token_env, "").strip()
        if not token:
            raise RuntimeError(f"音楽認識APIトークンが未設定です: 環境変数 {self._token_env}")
        wav = _wav_bytes(audio, sample_rate)
        boundary = f"----PoppoMusic{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend((
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"), b"\r\n",
            ))

        field("api_token", token)
        field("return", "spotify,apple_music")
        parts.extend((
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n", wav, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ))
        request = urllib.request.Request(
            self._endpoint,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_s))) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"音楽認識APIへ接続できません: {exc.reason}") from exc
        self.last_diagnostic = {
            "status": payload.get("status"),
            "error": payload.get("error"),
            "matched": isinstance(payload.get("result"), dict),
        }
        if payload.get("status") != "success":
            error = payload.get("error") or {}
            detail = error.get("error_message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"AudD APIエラー: {detail or payload.get('status')}")
        if not isinstance(payload.get("result"), dict):
            return None
        result = payload["result"]
        title, artist = str(result.get("title") or "").strip(), str(result.get("artist") or "").strip()
        if not title:
            return None
        return MusicRecognitionResult(
            title=title,
            artist=artist,
            album=str(result.get("album") or "").strip(),
            song_link=str(result.get("song_link") or "").strip(),
        )


class ACRCloudHummingRecognizer:
    """ACRCloud cover-song/humming recognition client.

    The ACRCloud project itself must have "Cover Song (humming) Identification"
    enabled; a standard fingerprint-only project cannot identify a hummed tune.
    """

    def __init__(
        self,
        *,
        host: str,
        access_key_env: str = "ACRCLOUD_ACCESS_KEY",
        access_secret_env: str = "ACRCLOUD_ACCESS_SECRET",
    ):
        self._host = str(host or "").strip().removeprefix("https://").removesuffix("/")
        self._access_key_env = access_key_env
        self._access_secret_env = access_secret_env

    @property
    def configured(self) -> bool:
        return bool(
            self._host
            and os.environ.get(self._access_key_env, "").strip()
            and os.environ.get(self._access_secret_env, "").strip()
        )

    def recognize(self, audio: np.ndarray, sample_rate: int, *, timeout_s: float = 15.0) -> MusicRecognitionResult | None:
        access_key = os.environ.get(self._access_key_env, "").strip()
        access_secret = os.environ.get(self._access_secret_env, "").strip()
        if not self._host or not access_key or not access_secret:
            raise RuntimeError("鼻歌認識のACRCloud接続情報が未設定です")
        timestamp = str(int(time.time()))
        to_sign = "\n".join(("POST", "/v1/identify", access_key, "audio", "1", timestamp))
        signature = base64.b64encode(
            hmac.new(access_secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
        wav = _wav_bytes(audio, sample_rate)
        boundary = f"----PoppoHumming{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend((
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"), b"\r\n",
            ))

        for name, value in (
            ("access_key", access_key), ("sample_bytes", str(len(wav))),
            ("timestamp", timestamp), ("signature", signature),
            ("data_type", "audio"), ("signature_version", "1"),
        ):
            field(name, value)
        parts.extend((
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="sample"; filename="humming.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n", wav, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ))
        request = urllib.request.Request(
            f"https://{self._host}/v1/identify", data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_s))) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"鼻歌認識APIへ接続できません: {exc.reason}") from exc
        metadata = payload.get("metadata") or {}
        candidates = metadata.get("humming") if isinstance(metadata, dict) else None
        if not isinstance(candidates, list) or not candidates:
            return None
        best = max(candidates, key=lambda item: float(item.get("score") or 0.0))
        artists = best.get("artists") or []
        artist = str((artists[0] or {}).get("name") or "").strip() if artists else ""
        title = str(best.get("title") or "").strip()
        if not title:
            return None
        return MusicRecognitionResult(
            title=title, artist=artist,
            album=str((best.get("album") or {}).get("name") or "").strip(),
        )
