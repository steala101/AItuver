"""外部 Vision API プロバイダ。

ローカル LLM が画像非対応の場合、スクリーンショットを外部 API で
テキスト説明に変換し、それを LLM のコンテキストへ注入する。

- claude: Anthropic Claude Vision (ANTHROPIC_API_KEY)
- google: Google Cloud Vision (GOOGLE_VISION_API_KEY) — OCR/ラベル検出
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_DESCRIBE_PROMPT = (
    "これはユーザーのPC画面のスクリーンショット。"
    "画面の中で一番大きく映っているメインのコンテンツ (動画・ゲーム・作業中のウィンドウ) を"
    "中心に、何が起きているかを日本語で具体的に説明して。"
    "動画なら映像の内容そのもの (誰が何をしているか・場面・雰囲気) を最優先で描写する。"
    "タスクバー・背景の別ウィンドウ・通知などの周辺要素は無視してよい。2〜4文で。"
)


class VisionProvider:
    """describe(jpeg_b64) -> str のインターフェース。"""

    name = "base"

    def describe(self, jpeg_b64: str) -> str | None:
        raise NotImplementedError


class ClaudeVisionProvider(VisionProvider):
    """Anthropic Claude による画面説明。精度が高い。"""

    name = "claude"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 300,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY が .env に未設定です")
        self._api_key = api_key
        self._model = model
        self._max_tokens = int(max_tokens)
        self._timeout = timeout

    def describe(self, jpeg_b64: str) -> str | None:
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": jpeg_b64,
                                },
                            },
                            {"type": "text", "text": _DESCRIBE_PROMPT},
                        ],
                    }],
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
            blocks = r.json().get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return text.strip() or None
        except Exception as e:
            logger.warning("Claude Vision に失敗: %s", e)
            return None


class GoogleVisionProvider(VisionProvider):
    """Google Cloud Vision による OCR + ラベル検出。"""

    name = "google"

    def __init__(self, api_key: str, timeout: float = 30.0):
        if not api_key:
            raise ValueError("GOOGLE_VISION_API_KEY が .env に未設定です")
        self._api_key = api_key
        self._timeout = timeout

    def describe(self, jpeg_b64: str) -> str | None:
        try:
            r = requests.post(
                f"https://vision.googleapis.com/v1/images:annotate?key={self._api_key}",
                json={
                    "requests": [{
                        "image": {"content": jpeg_b64},
                        "features": [
                            {"type": "LABEL_DETECTION", "maxResults": 8},
                            {"type": "TEXT_DETECTION"},
                        ],
                    }]
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
            res = (r.json().get("responses") or [{}])[0]
            parts = []
            labels = [x["description"] for x in res.get("labelAnnotations", [])]
            if labels:
                parts.append("画面の要素: " + ", ".join(labels))
            texts = res.get("textAnnotations")
            if texts:
                snippet = texts[0].get("description", "").strip().replace("\n", " ")
                if snippet:
                    parts.append("画面上のテキスト: " + snippet[:600])
            return "\n".join(parts) or None
        except Exception as e:
            logger.warning("Google Vision に失敗: %s", e)
            return None


def build_vision_provider(cfg) -> VisionProvider | None:
    """vision.provider の値に応じたプロバイダを返す。local の場合は None。"""
    provider = str(cfg.get("vision.provider", "local")).lower()
    if provider in ("", "local", "none"):
        return None
    if provider == "claude":
        c = cfg.section("vision.claude")
        return ClaudeVisionProvider(
            api_key=c.get("api_key", ""),
            model=c.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=int(c.get("max_tokens", 300)),
        )
    if provider == "google":
        g = cfg.section("vision.google")
        return GoogleVisionProvider(api_key=g.get("api_key", ""))
    raise ValueError(f"未定義のVisionプロバイダ: {provider}")
