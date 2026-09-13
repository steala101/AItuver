"""ランチャー → AI本体 の localhost 制御クライアント。

AI本体の AppControlServer (127.0.0.1) を叩く薄いHTTPクライアント。
共有シークレットは X-Control-Token ヘッダで送る (コマンドライン非経由)。
標準ライブラリのみ (urllib) で軽量に保つ。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class ControlClient:
    def __init__(self, token: str, *, host: str = "127.0.0.1", port: int = 8766):
        self._base = f"http://{host}:{port}"
        self._token = token

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float = 30.0) -> dict:
        data = None
        headers = {"X-Control-Token": self._token}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self._base + path, data=data, headers=headers, method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8")) if body else {}

    # 制御サーバーが応答すれば「正当なAI本体」と確認できる (指示書11章の同一性確認)。
    def health(self, timeout: float = 3.0) -> dict | None:
        try:
            return self._request("GET", "/health", timeout=timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def status(self) -> dict:
        return self._request("GET", "/status")

    def channels(self) -> dict:
        return self._request("GET", "/channels")

    def join(self, channel_id: str | None) -> dict:
        return self._request("POST", "/join", {"channel_id": channel_id or ""})

    def leave(self) -> dict:
        return self._request("POST", "/leave")

    def shutdown(self) -> dict:
        return self._request("POST", "/shutdown", timeout=10.0)

    # ペルソナ / 話者 / こころ
    def personas(self) -> dict:
        return self._request("GET", "/personas")

    def set_persona(self, key: str) -> dict:
        return self._request("POST", "/persona", {"key": key})

    def speakers(self) -> dict:
        return self._request("GET", "/speakers")

    def set_speaker(self, speaker_id: str) -> dict:
        return self._request("POST", "/speaker", {"speaker_id": speaker_id})

    def mind(self) -> dict:
        return self._request("GET", "/mind")

    # STT / TTS 設定 (切替系はモデルロードで重いので長めのタイムアウト)
    def stt_status(self) -> dict:
        return self._request("GET", "/stt")

    def set_stt_device(self, device: str) -> dict:
        return self._request("POST", "/stt/device", {"device": device}, timeout=120.0)

    def tts_status(self) -> dict:
        return self._request("GET", "/tts")

    def runtime_info(self, timeout: float = 3.0) -> dict:
        return self._request("GET", "/runtime", timeout=timeout)

    def set_tts_volume(self, volume: float) -> dict:
        return self._request("POST", "/tts/volume", {"volume": volume})

    def set_tts_engine(self, engine: str) -> dict:
        return self._request("POST", "/tts/engine", {"engine": engine}, timeout=120.0)
