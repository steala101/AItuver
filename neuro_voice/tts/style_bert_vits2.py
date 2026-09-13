"""Style-Bert-VITS2 (server_fastapi.py) による感情表現つきTTSバックエンド。

VOICEVOXと同じ TTSBackend インターフェースを実装する。
感情タグ (joy/sad等) と Speech Director の delivery を、モデルが持つ
スタイル (例: 小春音アミの「るんるん」「よふかし」「ささやき」) へ写像し、
style_weight / length (話速) / 音量ゲインで表現の強弱を付ける。
"""
from __future__ import annotations

import io
import logging
import threading
import time

import numpy as np
import requests
import soundfile as sf

from neuro_voice.tts.base import TTSBackend
from neuro_voice.tts.style import SpeechStyle, StyleManager
from neuro_voice.tts.pronunciation import apply_pronunciations, normalize_pronunciations

logger = logging.getLogger(__name__)

_DEFAULT_STYLE = "Neutral"


class StyleBertVits2TTS(TTSBackend):
    """Style-Bert-VITS2 の HTTP API (GET /voice) で音声合成する。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5000",
        model_name: str | None = None,
        speaker_id: int = 0,
        language: str = "JP",
        default_style: str = _DEFAULT_STYLE,
        style_weight: float = 1.0,
        lock_single_style_weight: bool = True,
        emotion_styles: dict | None = None,
        delivery_styles: dict | None = None,
        speed: float = 1.0,
        sdp_ratio: float = 0.2,
        noise: float = 0.6,
        noisew: float = 0.8,
        timeout: float = 30.0,
        auto_launch: bool = True,
        install_path: str | None = None,
        device: str = "cuda",
        pronunciations: dict | None = None,
        on_status=None,
    ):
        self._base = base_url.rstrip("/")
        self._model_name = (model_name or "").strip() or None
        self._speaker_id = int(speaker_id)
        self._language = str(language or "JP")
        self._default_style = str(default_style or _DEFAULT_STYLE)
        self._style_weight = float(style_weight)
        self._lock_single_style_weight = bool(lock_single_style_weight)
        self._emotion_styles = {str(k).lower(): str(v) for k, v in (emotion_styles or {}).items()}
        self._delivery_styles = {str(k).lower(): str(v) for k, v in (delivery_styles or {}).items()}
        self._speed = float(speed)
        self._sdp_ratio = float(sdp_ratio)
        self._noise = float(noise)
        self._noisew = float(noisew)
        self._timeout = timeout
        self._auto_launch = bool(auto_launch)
        self._install_path = install_path
        self._device = device
        self._on_status = on_status
        self._last_relaunch = 0.0
        self._session = requests.Session()
        self._style_manager = StyleManager()
        self._pronunciations = normalize_pronunciations(pronunciations)
        self._known_styles: set[str] | None = None  # None = 未取得 (起動待ちの可能性)
        self._models_info: dict[str, dict] = {}
        self._warned_styles: set[str] = set()
        self._load_model_info(log_connection_error=True)
        # 初回合成はモデルロード+CUDAカーネル初期化で数秒かかる。裏で温めて
        # 最初の応答の「TTS初回」を短くする (失敗しても会話へは波及させない)。
        self.prewarm()

    # ---------- サーバー情報・自己修復 ----------

    def _load_model_info(self, log_connection_error: bool = False) -> None:
        """/models/info からモデル名とスタイル一覧を取得する。"""
        try:
            r = self._session.get(f"{self._base}/models/info", timeout=5)
            r.raise_for_status()
            info = r.json() or {}
        except requests.RequestException:
            if log_connection_error:
                logger.warning(
                    "Style-Bert-VITS2 サーバーに接続できません (%s)。"
                    "自動起動が有効なら合成時に再試行します", self._base,
                )
            return
        names: list[str] = []
        styles: set[str] = set()
        for _mid, m in info.items():
            model = self._model_name_from_info(m)
            if model:
                names.append(model)
            if self._model_name is None or model == self._model_name:
                styles.update((m.get("style2id") or {}).keys())
        if self._model_name is None and names:
            self._model_name = names[0]
            logger.info("Style-Bert-VITS2: モデル未指定のため %s を使用", self._model_name)
        self._models_info = {str(k): dict(v or {}) for k, v in info.items()}
        self._known_styles = styles or None
        logger.info(
            "Style-Bert-VITS2 %s に接続 (model=%s, styles=%s)",
            self._base, self._model_name, sorted(styles) if styles else "?",
        )

    @staticmethod
    def _model_name_from_info(info: dict) -> str:
        configured = str(info.get("model_name", "") or "").strip()
        if configured:
            return configured
        parts = str(info.get("config_path", "") or "").replace("\\", "/").split("/")
        return parts[-2] if len(parts) >= 2 else ""

    @property
    def model_name(self) -> str | None:
        return self._model_name

    @property
    def speaker_id(self) -> int:
        return self._speaker_id

    @property
    def speaker(self) -> str:
        return f"{self._model_name or ''}::{self._speaker_id}"

    def list_speakers(self) -> list[dict]:
        """Return every loaded model/speaker pair from ``/models/info``."""
        self._load_model_info(log_connection_error=True)
        speakers: list[dict] = []
        for info in self._models_info.values():
            model = self._model_name_from_info(info)
            spk2id = info.get("spk2id") or {}
            if isinstance(spk2id, dict) and spk2id:
                for name, speaker_id in spk2id.items():
                    sid = int(speaker_id)
                    key = f"{model}::{sid}"
                    speakers.append({
                        "id": key, "key": key, "label": f"{model} / {name}",
                        "model_name": model, "speaker_id": sid,
                    })
            elif model:
                key = f"{model}::0"
                speakers.append({
                    "id": key, "key": key, "label": model,
                    "model_name": model, "speaker_id": 0,
                })
        return speakers

    def set_speaker(self, selection) -> None:
        """Select ``model::speaker_id`` (or an integer for the current model)."""
        model = self._model_name
        speaker_id = selection
        if isinstance(selection, str) and "::" in selection:
            model, speaker_id = selection.rsplit("::", 1)
        self._model_name = str(model or "").strip() or None
        self._speaker_id = int(speaker_id)
        styles: set[str] = set()
        for info in self._models_info.values():
            if self._model_name_from_info(info) == self._model_name:
                styles.update((info.get("style2id") or {}).keys())
        self._known_styles = styles or None
        logger.info(
            "Style-Bert-VITS2 話者を変更: model=%s speaker=%d",
            self._model_name, self._speaker_id,
        )
        # モデル切替後の初回合成はロードで重い。裏で温めておく
        self.prewarm()

    def prewarm(self, wait_server_s: float = 90.0) -> None:
        """短い合成を裏スレッドで実行し、モデルロード/CUDA初期化を先に済ませる。

        遠隔ヘッドレス起動ではサーバー自体がまだ裏で起動中のことがある。
        その場合 /models/info が応答するまで待ってから温める。これをしないと
        prewarm が空振りし、最初の実発話でモデルロードが走って数秒遅れる。
        """
        def _run() -> None:
            # サーバーの起動待ち (最大 wait_server_s)。応答が来たら温める。
            deadline = time.monotonic() + max(0.0, wait_server_s)
            while time.monotonic() < deadline:
                try:
                    r = requests.get(f"{self._base}/models/info", timeout=3)
                    if r.ok:
                        break
                except requests.RequestException:
                    time.sleep(2.0)
            try:
                params = {
                    "text": "ん", "speaker_id": self._speaker_id,
                    "language": self._language, "style": _DEFAULT_STYLE,
                    "length": 1.0, "auto_split": True,
                }
                if self._model_name:
                    params["model_name"] = self._model_name
                # 本番合成と競合しないよう Session は共有しない
                r = requests.get(f"{self._base}/voice", params=params, timeout=60)
                if r.ok:
                    logger.info("Style-Bert-VITS2 prewarm完了 (model=%s)", self._model_name)
            except Exception:
                logger.debug("Style-Bert-VITS2 prewarm失敗 (無視)", exc_info=True)

        threading.Thread(target=_run, name="sbv2-prewarm", daemon=True).start()

    def _try_relaunch(self) -> bool:
        """接続不可時にサーバーの (再) 起動を試みる。クールダウン付き。"""
        if not self._auto_launch:
            return False
        now = time.monotonic()
        if now - self._last_relaunch < 30.0:
            return False
        self._last_relaunch = now
        logger.warning("Style-Bert-VITS2 に接続できないため、サーバーの再起動を試みます")
        if self._on_status is not None:
            try:
                self._on_status("Style-Bert-VITS2 に接続できません。起動しています...")
            except Exception:
                pass
        from neuro_voice.tts.style_bert_vits2_launcher import ensure_server

        ok = ensure_server(
            self._base,
            self._install_path,
            device=self._device,
            on_status=self._on_status,
        )
        if ok:
            self._load_model_info()
        return ok

    def ensure_ready(self) -> bool:
        """初回発話前の疎通確認 (Discord経路が利用)。"""
        try:
            r = self._session.get(f"{self._base}/models/info", timeout=3)
            r.raise_for_status()
            if self._known_styles is None:
                self._load_model_info()
            return True
        except requests.RequestException:
            return self._try_relaunch()

    def set_pronunciations(self, pronunciations: dict | None) -> None:
        self._pronunciations = normalize_pronunciations(pronunciations)

    def close(self) -> None:
        self._session.close()

    # ---------- スタイル解決 ----------

    def _resolve_style(self, style: SpeechStyle) -> str:
        """delivery → 感情 → 既定 の順でモデルのスタイル名へ写像する。"""
        candidates = [
            self._delivery_styles.get(style.delivery),
            self._emotion_styles.get(style.emotion),
            self._default_style,
            _DEFAULT_STYLE,
        ]
        for name in candidates:
            if not name:
                continue
            if self._known_styles is None or name in self._known_styles:
                return name
            if name not in self._warned_styles:
                self._warned_styles.add(name)
                logger.warning(
                    "Style-Bert-VITS2: スタイル「%s」はモデル %s に存在しません "
                    "(利用可能: %s)", name, self._model_name,
                    sorted(self._known_styles or []),
                )
        return _DEFAULT_STYLE

    # ---------- 合成 ----------

    def synthesize(
        self, text: str, emotion: str | None = None, style: SpeechStyle | None = None,
    ) -> tuple[np.ndarray, int]:
        start = time.perf_counter()
        spoken_text = apply_pronunciations(text, self._pronunciations)
        style = style or self._style_manager.resolve(emotion, None)
        sbv2_style = self._resolve_style(style)

        # length は話速の逆数 (大きいほど遅い)。感情の強さは style_weight を増減。
        speed = max(0.5, min(2.0, self._speed * style.speed))
        # A model with only one style has no alternate style vector to lean
        # toward.  Moving its weight every turn merely distorts the sole voice
        # embedding and can make articulation unstable.  Keep the configured
        # neutral weight for tsukuyomi-like models; multi-style models still
        # receive the intended emotional intensity.
        single_style_locked = bool(
            self._lock_single_style_weight
            and self._known_styles
            and len(self._known_styles) == 1
        )
        weight_scale = 1.0 if single_style_locked else style.intonation
        weight = max(0.0, min(3.0, self._style_weight * weight_scale))
        length = 1.0 / speed
        params = {
            "text": spoken_text,
            "speaker_id": self._speaker_id,
            "language": self._language,
            "style": sbv2_style,
            "style_weight": round(weight, 3),
            "length": round(length, 3),
            "sdp_ratio": self._sdp_ratio,
            "noise": self._noise,
            "noisew": self._noisew,
            "auto_split": True,
        }
        if self._model_name:
            params["model_name"] = self._model_name

        try:
            r = self._session.get(f"{self._base}/voice", params=params, timeout=self._timeout)
        except requests.ConnectionError:
            # サーバーが落ちている → 起動を試みて1回だけリトライ (自己修復)
            if not self._try_relaunch():
                raise
            r = self._session.get(f"{self._base}/voice", params=params, timeout=self._timeout)
        r.raise_for_status()

        wav, sr = sf.read(io.BytesIO(r.content), dtype="float32")
        if wav.ndim > 1:
            wav = wav[:, 0]
        # SBV2 に音量パラメータはないため、波形ゲインで volume を反映する
        gain = max(0.0, min(2.0, style.volume))
        if abs(gain - 1.0) > 1e-3:
            wav = np.clip(wav * gain, -1.0, 1.0)
        logger.info(
            "TTS合成(SBV2) %.2fs (%d文字, 音声%.1f秒, style=%s w=%.2f "
            "speed=%.2f length=%.2f single_style=%s): %s",
            time.perf_counter() - start, len(text), len(wav) / sr,
            sbv2_style, weight, speed, length, single_style_locked, text[:20],
        )
        return np.ascontiguousarray(wav, dtype=np.float32), int(sr)
