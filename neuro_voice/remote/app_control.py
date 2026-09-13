"""AI本体 (--discordモード) 側の localhost 限定 制御サーバー。

指示書7章・8章に対応。ランチャーからの内部制御だけを受け付ける:
  GET  /health   … Ready判定 (プロセス存在ではなく本体からの明示的Ready)
  GET  /status   … 接続・VC状況
  GET  /channels … 参加可能なVC一覧
  POST /join     … 指定VCへ参加      {"channel_id": "..."}
  POST /leave    … VCから退出
  POST /shutdown … 本体の正常終了を要求 (受理を返し、実処理は本体ループが行う)

セキュリティ:
  - 127.0.0.1 / ::1 のみで待ち受ける (Tailscale/LANへは絶対に公開しない)
  - 共有シークレット (X-Control-Token) を必須にする
  - シークレットは環境変数 AI_APP_CONTROL_TOKEN で受け取る (コマンドライン非経由)
  - シークレットや会話内容はログに出さない
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_MAX_BODY = 4096  # 入力サイズ上限 (指示書18章)
_CHANNEL_ID_MAX = 32


def _valid_snowflake(value: str) -> bool:
    return bool(value) and value.isdigit() and len(value) <= _CHANNEL_ID_MAX


class AppControlServer:
    """AI本体のイベントループへ安全に橋渡しする最小HTTP制御サーバー。"""

    def __init__(
        self,
        bridge,
        loop: asyncio.AbstractEventLoop,
        *,
        token: str,
        request_shutdown: Callable[[], None],
        host: str = "127.0.0.1",
        port: int = 8766,
    ):
        self._bridge = bridge
        self._loop = loop
        self._token = token
        self._request_shutdown = request_shutdown
        self._host = host
        self._port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---------- 起動・停止 ----------

    def start(self) -> None:
        if self._host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError("制御サーバーは localhost 以外へバインドできません")
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="app-control", daemon=True,
        )
        self._thread.start()
        logger.info("AI本体 制御サーバーを起動: http://%s:%d", self._host, self._port)

    def stop(self) -> None:
        if self._httpd is not None:
            with __import__("contextlib").suppress(Exception):
                self._httpd.shutdown()
                self._httpd.server_close()
        self._httpd = None

    # ---------- ループへの橋渡し ----------

    def _run_coro(self, coro, timeout: float = 30.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _run_sync(self, fn, timeout: float = 30.0):
        """同期メソッドをAI本体のイベントループ上で実行する。

        ペルソナ切替・話者変更・こころ取得は mind/tts の状態に触れるため、
        音声処理と同じループ上で直列化して競合を避ける。
        """
        async def _wrap():
            return fn()

        future = asyncio.run_coroutine_threadsafe(_wrap(), self._loop)
        return future.result(timeout=timeout)

    # ---------- ハンドラ本体 (別スレッドから呼ばれる) ----------

    def health(self) -> dict:
        status = self._bridge.voice_status()
        return {"ready": bool(status.get("connected")), **status}

    def status(self) -> dict:
        return self._bridge.voice_status()

    def channels(self) -> dict:
        return {"channels": self._bridge.list_voice_channels()}

    def join(self, channel_id: str) -> dict:
        cid = str(channel_id or "").strip()
        if cid and not _valid_snowflake(cid):
            return {"ok": False, "message": "channel_id の形式が不正です"}
        result = self._run_coro(self._bridge.join_channel(cid or None))
        if result.get("ok"):
            result.update(self._bridge.voice_status())
        return result

    def leave(self) -> dict:
        return self._run_coro(self._bridge.leave())

    def shutdown(self) -> dict:
        # 実際の順序終了は本体のメインループが行う。ここでは受理だけ返す。
        self._request_shutdown()
        return {"accepted": True}

    # ---------- ペルソナ / 話者 / こころ ----------

    def personas(self) -> dict:
        return self._bridge.list_personas()

    def set_persona(self, key: str) -> dict:
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "message": "ペルソナが指定されていません"}
        return self._run_sync(lambda: self._bridge.switch_persona(key))

    def speakers(self) -> dict:
        return self._bridge.list_tts_speakers()

    def set_speaker(self, speaker_id: str) -> dict:
        sid = str(speaker_id or "").strip()
        if not sid:
            return {"ok": False, "message": "話者が指定されていません"}
        return self._run_sync(lambda: self._bridge.set_tts_speaker(sid))

    def mind(self) -> dict:
        return self._run_sync(lambda: self._bridge.mind_status())

    # ---------- STT / TTS 設定 ----------
    # 読み取り(status)は軽いのでそのまま。切替系はモデルロードで重く、
    # asyncioループを塞ぐと再生が止まるため、HTTPスレッドで同期実行する。

    def stt_status(self) -> dict:
        return self._bridge.stt_status()

    def reload_stt(self, device: str) -> dict:
        device = str(device or "").strip()
        if not device:
            return {"ok": False, "message": "device が指定されていません"}
        return self._bridge.reload_stt(device)

    def tts_status(self) -> dict:
        return self._bridge.tts_status()

    def runtime_info(self) -> dict:
        return self._bridge.runtime_info()

    def set_tts_volume(self, volume) -> dict:
        return self._bridge.set_tts_volume(volume)

    def set_tts_engine(self, engine: str) -> dict:
        engine = str(engine or "").strip()
        if not engine:
            return {"ok": False, "message": "engine が指定されていません"}
        return self._bridge.set_tts_engine(engine)


def _make_handler(server: "AppControlServer"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # noqa: N802 - アクセスログは出さない
            return

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            token = self.headers.get("X-Control-Token", "")
            # バイト列で比較 (非ASCIIが来ても例外にせず不一致にする) + 定数時間比較
            import hmac

            if not server._token:
                return False
            try:
                return hmac.compare_digest(
                    token.encode("utf-8"), server._token.encode("utf-8"))
            except Exception:
                return False

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            if length > _MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")) if raw else {}

        def do_GET(self):  # noqa: N802
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            try:
                if self.path == "/health":
                    return self._send(200, server.health())
                if self.path == "/status":
                    return self._send(200, server.status())
                if self.path == "/channels":
                    return self._send(200, server.channels())
                if self.path == "/personas":
                    return self._send(200, server.personas())
                if self.path == "/speakers":
                    return self._send(200, server.speakers())
                if self.path == "/mind":
                    return self._send(200, server.mind())
                if self.path == "/stt":
                    return self._send(200, server.stt_status())
                if self.path == "/tts":
                    return self._send(200, server.tts_status())
                if self.path == "/runtime":
                    return self._send(200, server.runtime_info())
                return self._send(404, {"error": "not found"})
            except Exception:
                logger.exception("制御サーバー GET でエラー")
                return self._send(500, {"error": "internal error"})

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            try:
                body = self._read_json()
            except Exception:
                return self._send(400, {"error": "bad request"})
            try:
                if self.path == "/join":
                    return self._send(200, server.join(str(body.get("channel_id", ""))))
                if self.path == "/leave":
                    return self._send(200, server.leave())
                if self.path == "/shutdown":
                    return self._send(200, server.shutdown())
                if self.path == "/persona":
                    return self._send(200, server.set_persona(str(body.get("key", ""))))
                if self.path == "/speaker":
                    return self._send(200, server.set_speaker(str(body.get("speaker_id", ""))))
                if self.path == "/stt/device":
                    return self._send(200, server.reload_stt(str(body.get("device", ""))))
                if self.path == "/tts/volume":
                    return self._send(200, server.set_tts_volume(body.get("volume")))
                if self.path == "/tts/engine":
                    return self._send(200, server.set_tts_engine(str(body.get("engine", ""))))
                return self._send(404, {"error": "not found"})
            except Exception:
                logger.exception("制御サーバー POST でエラー")
                return self._send(500, {"error": "internal error"})

    return Handler
