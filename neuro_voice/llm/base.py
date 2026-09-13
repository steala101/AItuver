"""LLM バックエンドの抽象インターフェース。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from neuro_voice.media import MediaInput


class LLMBackend(ABC):
    """generate(messages) を実装すれば任意のバックエンドを追加できる。"""

    name: str = "base"

    @abstractmethod
    def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        """メッセージ列からトークンを逐次生成する非同期イテレータを返す。"""

    async def generate_with_media(
        self, messages: list[dict[str, Any]], media: list[MediaInput], *, request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Generate with media when the backend supports it."""
        raise RuntimeError(f"{self.name} does not support multimodal input")

    def cancel_request(self, request_id: str | None) -> None:
        """Best-effort request cancellation; text-only backends may ignore it."""

    async def unload(self) -> None:
        """終了時のクリーンアップ (ローカルLLMのVRAM解放など)。既定は何もしない。"""
