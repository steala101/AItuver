"""OBS Studio game-source capture through the bundled obs-websocket server.

OBS 28 and newer include obs-websocket v5.  This module intentionally uses the
protocol directly instead of OBS Virtual Camera: the assistant only needs a
fresh still frame on demand, and ``GetSourceScreenshot`` avoids opening another
camera device or affecting the user's mouse/game window.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from typing import Any
from uuid import uuid4

from neuro_voice.media import MediaInput, MediaType
from neuro_voice.vision.media import prepare_image

logger = logging.getLogger(__name__)


class ObsError(RuntimeError):
    """Base error for actionable OBS capture failures."""


class ObsConnectionError(ObsError):
    """OBS is not reachable or the websocket handshake failed."""


class ObsRequestError(ObsError):
    """OBS rejected a protocol request."""


def build_authentication(password: str, salt: str, challenge: str) -> str:
    """Return the obs-websocket v5 SHA256 challenge response."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


def decode_image_data(image_data: str) -> bytes:
    """Decode OBS's data-URI screenshot response with useful validation."""
    value = str(image_data or "")
    if not value:
        raise ObsRequestError("OBSから画像データが返りませんでした。ソースが有効か確認してください。")
    encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ObsRequestError("OBSから返された画像データを復号できませんでした。") from exc


class ObsWebSocketClient:
    """Small serialized obs-websocket v5 client with reconnect-once behavior."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 4455, password: str = "",
        *, timeout: float = 3.0,
    ):
        self.host = str(host or "127.0.0.1").strip()
        self.port = int(port)
        self.password = str(password or "")
        self.timeout = max(.5, float(timeout))
        self._socket = None
        self._lock = asyncio.Lock()
        self.server_info: dict[str, Any] = {}

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def connect(self) -> None:
        async with self._lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        if self._socket is not None:
            return
        try:
            import websockets

            socket = await asyncio.wait_for(
                websockets.connect(
                    self.url, open_timeout=self.timeout, close_timeout=1,
                    ping_interval=20, ping_timeout=10, max_size=16 * 1024 * 1024,
                ),
                timeout=self.timeout,
            )
            hello = json.loads(await asyncio.wait_for(socket.recv(), timeout=self.timeout))
            if int(hello.get("op", -1)) != 0:
                raise ObsConnectionError("OBS WebSocketのHelloを受信できませんでした。")
            hello_data = hello.get("d") or {}
            identify: dict[str, Any] = {
                "rpcVersion": min(1, int(hello_data.get("rpcVersion", 1))),
                # We only issue requests; no OBS events are needed.
                "eventSubscriptions": 0,
            }
            auth = hello_data.get("authentication")
            if isinstance(auth, dict):
                if not self.password:
                    raise ObsConnectionError(
                        "OBS WebSocketのパスワードが必要です。設定画面へ入力してください。"
                    )
                identify["authentication"] = build_authentication(
                    self.password, str(auth.get("salt", "")), str(auth.get("challenge", "")),
                )
            await socket.send(json.dumps({"op": 1, "d": identify}))
            identified = json.loads(await asyncio.wait_for(socket.recv(), timeout=self.timeout))
            if int(identified.get("op", -1)) != 2:
                raise ObsConnectionError("OBS WebSocketの認証に失敗しました。パスワードを確認してください。")
            self._socket = socket
            self.server_info = {
                "obs_version": hello_data.get("obsStudioVersion", ""),
                "websocket_version": hello_data.get("obsWebSocketVersion", ""),
                "rpc_version": (identified.get("d") or {}).get("negotiatedRpcVersion", 1),
            }
            logger.info(
                "OBS WebSocket connected: %s OBS=%s websocket=%s",
                self.url, self.server_info["obs_version"], self.server_info["websocket_version"],
            )
        except ObsError:
            if "socket" in locals():
                await socket.close()
            raise
        except Exception as exc:
            if "socket" in locals():
                try:
                    await socket.close()
                except Exception:
                    pass
            raise ObsConnectionError(
                f"OBSへ接続できません ({self.url})。OBSとWebSocketサーバーを起動してください: {exc}"
            ) from exc

    async def close(self) -> None:
        async with self._lock:
            socket, self._socket = self._socket, None
            if socket is not None:
                try:
                    await socket.close()
                except Exception:
                    logger.debug("OBS WebSocket close failed", exc_info=True)

    async def request(self, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue one request; reconnect once if OBS restarted underneath us."""
        last_error: Exception | None = None
        for attempt in range(2):
            async with self._lock:
                try:
                    await self._connect_locked()
                    return await self._request_locked(request_type, request_data)
                except ObsRequestError:
                    raise
                except Exception as exc:
                    last_error = exc
                    socket, self._socket = self._socket, None
                    if socket is not None:
                        try:
                            await socket.close()
                        except Exception:
                            pass
            if attempt == 0:
                logger.info("OBS request connection lost; reconnecting once")
        if isinstance(last_error, ObsError):
            raise last_error
        raise ObsConnectionError(f"OBSとの通信が切断されました: {last_error}") from last_error

    async def _request_locked(
        self, request_type: str, request_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        socket = self._socket
        if socket is None:
            raise ObsConnectionError("OBS WebSocketが未接続です。")
        request_id = uuid4().hex
        payload: dict[str, Any] = {
            "op": 6,
            "d": {"requestType": request_type, "requestId": request_id},
        }
        if request_data:
            payload["d"]["requestData"] = request_data
        await asyncio.wait_for(socket.send(json.dumps(payload)), timeout=self.timeout)
        deadline = asyncio.get_running_loop().time() + self.timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ObsConnectionError(f"OBSの応答がタイムアウトしました: {request_type}")
            message = json.loads(await asyncio.wait_for(socket.recv(), timeout=remaining))
            if int(message.get("op", -1)) != 7:
                continue
            data = message.get("d") or {}
            if str(data.get("requestId", "")) != request_id:
                continue
            status = data.get("requestStatus") or {}
            if not bool(status.get("result", False)):
                comment = str(status.get("comment", "") or "OBSが要求を拒否しました")
                raise ObsRequestError(
                    f"OBS {request_type} エラー ({status.get('code', '?')}): {comment}"
                )
            return data.get("responseData") or {}

    async def probe(self) -> dict[str, Any]:
        version = await self.request("GetVersion")
        return {**self.server_info, **version}

    async def list_inputs(self) -> list[dict[str, Any]]:
        data = await self.request("GetInputList")
        inputs = data.get("inputs") or []
        return [item for item in inputs if isinstance(item, dict)]


class ObsCapture:
    """Captures the current render of one named OBS source."""

    PREFERRED_KINDS = ("game_capture", "window_capture", "monitor_capture", "display_capture")

    def __init__(
        self, host: str, port: int, password: str, source_name: str,
        *, max_width: int = 960, jpeg_quality: int = 75, timeout: float = 3.0,
    ):
        self.client = ObsWebSocketClient(host, port, password, timeout=timeout)
        self.source_name = str(source_name or "").strip()
        self.max_width = max(64, min(4096, int(max_width)))
        self.jpeg_quality = max(1, min(100, int(jpeg_quality)))
        self._sequence = 0
        self._last_active_check = 0.0

    async def start(self) -> None:
        await self.client.connect()
        inputs = await self.client.list_inputs()
        if self.source_name:
            if not any(str(item.get("inputName", "")) == self.source_name for item in inputs):
                raise ObsRequestError(
                    f"OBSソース「{self.source_name}」が見つかりません。設定から選び直してください。"
                )
            return
        preferred = [
            item for item in inputs
            if str(item.get("unversionedInputKind") or item.get("inputKind") or "") in self.PREFERRED_KINDS
        ]
        if len(preferred) == 1:
            self.source_name = str(preferred[0].get("inputName", ""))
            logger.info("OBS capture source auto-selected: %s", self.source_name)
            return
        if not preferred:
            raise ObsRequestError(
                "OBSにゲームキャプチャソースがありません。OBSで「ゲームキャプチャ」を追加してください。"
            )
        raise ObsRequestError("OBSソースが複数あります。設定画面で使用するソースを選んでください。")

    async def close(self) -> None:
        await self.client.close()

    async def capture_media(self) -> MediaInput:
        if not self.source_name:
            await self.start()
        started = time.perf_counter()
        # Source visibility changes rarely. Checking once per frame doubled
        # websocket round trips, so refresh it periodically and let screenshot
        # failures trigger the normal reconnect/error path in between.
        if time.monotonic() - self._last_active_check >= 5.0:
            active = await self.client.request("GetSourceActive", {"sourceName": self.source_name})
            self._last_active_check = time.monotonic()
            if not bool(active.get("videoActive") or active.get("videoShowing")):
                raise ObsRequestError(
                    f"OBSソース「{self.source_name}」が現在のシーンで非表示です。OBS側で表示してください。"
                )
        result = await self.client.request("GetSourceScreenshot", {
            "sourceName": self.source_name,
            "imageFormat": "jpg",
            "imageWidth": self.max_width,
            "imageCompressionQuality": self.jpeg_quality,
        })
        response_at = time.perf_counter()
        raw = decode_image_data(str(result.get("imageData", "")))
        decoded_at = time.perf_counter()
        captured_at = time.time()
        image = prepare_image(
            raw, max_width=self.max_width, max_height=4096,
            jpeg_quality=self.jpeg_quality, media_type=MediaType.VIDEO_FRAME,
            timestamp=captured_at,
        )
        self._sequence += 1
        finished = time.perf_counter()
        image.metadata.update({
            "source": "obs",
            "obs_source_name": self.source_name,
            "capture_backend": "obs-websocket",
            "capture_sequence": self._sequence,
            "capture_latency_ms": round((finished - started) * 1000),
            "frame_capture_ms": round((response_at - started) * 1000),
            "frame_decode_ms": round((decoded_at - response_at) * 1000),
            "frame_encode_ms": round((finished - decoded_at) * 1000),
            "fresh_request": True,
        })
        return image

    async def list_sources(self) -> list[dict[str, Any]]:
        await self.client.connect()
        items = await self.client.list_inputs()
        preferred = set(self.PREFERRED_KINDS)
        result = []
        for item in items:
            kind = str(item.get("unversionedInputKind") or item.get("inputKind") or "")
            result.append({
                "name": str(item.get("inputName", "")),
                "kind": kind,
                "recommended": kind in preferred,
            })
        return sorted(result, key=lambda item: (not item["recommended"], item["name"].lower()))
