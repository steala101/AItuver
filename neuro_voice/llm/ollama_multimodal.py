"""Single boundary for Ollama multimodal requests.

The rest of the application passes :class:`MediaInput`; only this module
serializes image bytes for Ollama's ``/api/chat`` REST payload.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from neuro_voice.media import MediaInput, MediaType

logger = logging.getLogger(__name__)


class OllamaMultimodalError(RuntimeError):
    pass


class OllamaMediaUnsupportedError(OllamaMultimodalError):
    pass


@dataclass(slots=True)
class OllamaCapabilities:
    connected: bool = False
    version: str | None = None
    model_present: bool = False
    image_input: bool = False
    audio_input: bool = False
    detail: str = "not probed"


class OllamaMultimodalClient:
    """Ollama REST client with cancellation-safe streaming and retries."""

    def __init__(
        self, base_url: str, model: str, *, timeout_s: float = 45.0,
        keep_alive: str | None = None, retries: int = 1,
    ):
        self._root = base_url.rstrip("/").removesuffix("/v1")
        self._model = model
        self._timeout_s = max(1.0, float(timeout_s))
        self._keep_alive = keep_alive or None
        self._retries = max(0, int(retries))
        self._cancelled: set[str] = set()
        self._model_capabilities: set[str] | None = None
        self._client = None
        self._client_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model

    def cancel(self, request_id: str | None) -> None:
        if request_id:
            self._cancelled.add(request_id)

    async def _http_client(self):
        """Reuse one connection pool instead of reconnecting for every frame."""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                import httpx

                timeout = httpx.Timeout(self._timeout_s, connect=min(10.0, self._timeout_s))
                self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def aclose(self) -> None:
        async with self._client_lock:
            client, self._client = self._client, None
            if client is not None:
                await client.aclose()

    async def unload(self) -> None:
        """Release this vision model from Ollama, then close HTTP resources."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self._root}/api/generate",
                    json={"model": self._model, "keep_alive": 0},
                )
                response.raise_for_status()
            logger.info("Ollama multimodal model unloaded: %s", self._model)
        except Exception:
            logger.warning("Ollama multimodal model unload failed: %s", self._model)
        finally:
            await self.aclose()

    @staticmethod
    def _message_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return str(content or "")

    def _messages(self, messages: list[dict[str, Any]], media: list[MediaInput] | None) -> list[dict[str, Any]]:
        payload = [
            {"role": str(m.get("role", "user")), "content": self._message_content(m.get("content"))}
            for m in messages
        ]
        if not media:
            return payload
        images: list[str] = []
        for item in media:
            if item.media_type not in (MediaType.IMAGE, MediaType.VIDEO_FRAME):
                raise OllamaMediaUnsupportedError(
                    f"Ollama media input does not support {item.media_type.value} in this backend"
                )
            images.append(base64.b64encode(item.read_bytes()).decode("ascii"))
        if not images:
            return payload
        if not payload:
            payload.append({"role": "user", "content": ""})
        payload[-1] = {**payload[-1], "images": images}
        return payload

    async def _ensure_image_capability(self) -> None:
        """Fail clearly instead of letting a text-only model invent an image answer."""
        if self._model_capabilities is None:
            try:
                client = await self._http_client()
                response = await client.post(f"{self._root}/api/show", json={"model": self._model})
                if response.is_error:
                    text = await response.aread()
                    raise OllamaMultimodalError(
                        f"Ollama could not inspect model '{self._model}': "
                        f"HTTP {response.status_code}: {text[:500]!r}"
                    )
                self._model_capabilities = {
                    str(value).lower() for value in (response.json().get("capabilities") or [])
                }
            except OllamaMultimodalError:
                raise
            except Exception as exc:
                raise OllamaMultimodalError(
                    f"Ollama could not inspect image capability for '{self._model}': {exc}"
                ) from exc
        if "vision" not in self._model_capabilities:
            raise OllamaMediaUnsupportedError(
                f"Ollama model '{self._model}' does not support image input. "
                "Select a model whose `ollama show` capabilities include `vision`, "
                "or choose an external Vision provider."
            )

    async def chat(
        self, messages: list[dict[str, Any]], media: list[MediaInput] | None = None,
        *, stream: bool = True, request_id: str | None = None,
        options: dict[str, Any] | None = None, think: bool | None = None,
    ) -> AsyncIterator[str]:
        """Yield response text. Cancelling the caller closes the HTTP stream."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - startup dependency check handles this
            raise OllamaMultimodalError("httpx is required for Ollama multimodal requests") from exc
        if media:
            await self._ensure_image_capability()
        body: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages(messages, media),
            "stream": bool(stream),
        }
        if self._keep_alive:
            body["keep_alive"] = self._keep_alive
        if options:
            # Dedicated visual-state inference uses a short output and compact
            # context.  Main conversation generation keeps its existing
            # settings because it calls this method without ``options``.
            body["options"] = dict(options)
        if think is not None:
            body["think"] = bool(think)
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            if request_id in self._cancelled:
                return
            try:
                client = await self._http_client()
                async with client.stream("POST", f"{self._root}/api/chat", json=body) as response:
                    if response.status_code in (400, 404) and media:
                        text = await response.aread()
                        raise OllamaMediaUnsupportedError(
                            f"Ollama model '{self._model}' rejected image input: {text[:240]!r}"
                        )
                    if response.is_error:
                        # Preserve the runner's response body.  In
                        # particular, OpenAICompatBackend needs the
                        # Windows llama-server crash signature to switch
                        # away from a broken QAT GGUF safely.
                        text = await response.aread()
                        raise OllamaMultimodalError(
                            f"Ollama HTTP {response.status_code}: {text[:1000]!r}"
                        )
                    if not stream:
                        data = await response.json()
                        text = str((data.get("message") or {}).get("content", ""))
                        if text:
                            yield text
                        return
                    async for line in response.aiter_lines():
                        if request_id in self._cancelled:
                            return
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        text = str((data.get("message") or {}).get("content", ""))
                        if text:
                            yield text
                        if data.get("done"):
                            return
                return
            except asyncio.CancelledError:
                self.cancel(request_id)
                raise
            except OllamaMediaUnsupportedError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self._retries:
                    break
                await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))
        raise OllamaMultimodalError(f"Ollama request failed: {last_error}")

    async def probe(self, *, check_image: bool = False, check_audio: bool = False) -> OllamaCapabilities:
        """Probe connection/model safely. Audio remains false unless supported.

        Ollama GGUF image support is model-dependent. A real image inference is
        opt-in because it may load a 12B model; audio is never assumed from a
        model name and is reported unavailable until a backend supports it.
        """
        caps = OllamaCapabilities()
        try:
            client = await self._http_client()
            version = await client.get(f"{self._root}/api/version")
            version.raise_for_status()
            caps.connected = True
            caps.version = str(version.json().get("version") or "") or None
            tags = await client.get(f"{self._root}/api/tags")
            tags.raise_for_status()
            names = {str(item.get("name", "")) for item in tags.json().get("models", [])}
            caps.model_present = self._model in names
            caps.detail = "model found" if caps.model_present else "model not found"
            if check_image and caps.model_present:
                # A tiny valid PNG verifies the actual model/API path. It
                # is deliberately used only for an explicit connection
                # test, not every application startup.
                tiny_png = (
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4"
                    "z8DwHwAFgAI/ScL9WQAAAABJRU5ErkJggg=="
                )
                image_test = await client.post(
                    f"{self._root}/api/chat",
                    json={
                        "model": self._model, "stream": False,
                        "messages": [{"role": "user", "content": "画像入力の疎通確認です。OKだけ返してください。", "images": [tiny_png]}],
                    },
                )
                caps.image_input = image_test.is_success
                if not caps.image_input:
                    caps.detail += f"; image rejected: {image_test.status_code}"
        except Exception as exc:
            caps.detail = f"connection failed: {exc}"
            return caps
        # Do not claim audio support for an image-capable model.  Current
        # Ollama/GGUF deployments often do not expose native audio inputs.
        caps.audio_input = False
        if check_audio:
            caps.detail += "; native audio input unavailable through this Ollama adapter"
        return caps
