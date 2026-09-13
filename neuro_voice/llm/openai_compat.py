"""OpenAI互換API バックエンド (Cerebras / OpenAI / OpenRouter / Ollama 共通)。"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from neuro_voice.llm.base import LLMBackend
from neuro_voice.media import MediaInput

logger = logging.getLogger(__name__)


class _InFlight:
    """How many generation requests are outstanding across all backends.

    A local server processes them one at a time, so a request that arrives
    while another is running waits for it (D-017).  If the fixed ~1114ms
    before the first token turns out to be queueing rather than model work,
    this counter is what shows it.
    """

    def __init__(self) -> None:
        self._count = 0
        #: When the currently running request started, if any.
        self._running_since: float | None = None
        #: How long the request already in progress had been running when the
        #: newest one arrived.  Zero when the server was idle.
        self.last_wait_ms = 0.0

    def enter(self) -> int:
        import time

        now = time.perf_counter()
        if self._count > 0 and self._running_since is not None:
            self.last_wait_ms = (now - self._running_since) * 1000
        else:
            self.last_wait_ms = 0.0
            self._running_since = now
        self._count += 1
        return self._count

    def leave(self) -> None:
        import time

        self._count = max(0, self._count - 1)
        self._running_since = time.perf_counter() if self._count else None


#: Shared on purpose: chat, vision and reflection all contend for one server.
_inflight = _InFlight()


class OpenAICompatBackend(LLMBackend):
    """base_url / api_key / model の差し替えだけで動く汎用実装。"""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 512,
        reasoning_effort: str | None = None,
        keep_alive: str | None = None,
        timeout_s: float = 45.0,
        multimodal_context_size: int = 4096,
        multimodal_max_tokens: int = 220,
        multimodal_temperature: float = 0.65,
        ollama_fallback_model: str | None = None,
        auto_fallback_on_runner_crash: bool = True,
        num_ctx: int = 4096,
        send_ollama_options: bool = True,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
    ):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "dummy")
        self._configured_model = model
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        #: 出力のばらつきを決める残りのつまみ。**未指定なら送らない。**
        #:
        #: 送らなければモデル自身の既定が使われる（Gemma 4 は top_k 64 /
        #: top_p 0.95 / temperature 1.0）。中途半端な値を勝手に送ると、
        #: モデルが想定している組み合わせを崩す。温度だけ動かして様子を
        #: 見たい時に、他が固定されていないと切り分けができない。
        self._top_p = None if top_p is None else float(top_p)
        self._top_k = None if top_k is None else int(top_k)
        self._min_p = None if min_p is None else float(min_p)
        # 会話用コンテキスト長。音声会話は短いので既定4096でVRAM(KVキャッシュ)を節約。
        # Ollama側は OLLAMA_CONTEXT_LENGTH でも制御 (SetupOllamaEnv 参照)。
        self._num_ctx = max(512, int(num_ctx))
        self._is_ollama = name.lower() == "ollama"
        self.last_generation_metrics: dict[str, Any] = {}
        #: Whether to send per-request options to Ollama.  A switch, not a
        #: setting: it exists to find out what the fixed ~1100ms is.
        self._send_options = bool(send_ollama_options)
        # OpenAI互換APIでは reasoning_effort="none" が思考OFF。Ollamaの
        # native API用 think:false はこのエンドポイントでは使わない。
        # 本文へ内部推論が混ざるモデルには SpokenOutputFilter も併用する。
        configured_effort = (reasoning_effort or "").strip().lower() or None
        self._reasoning_effort = configured_effort or (
            "none" if name.lower() == "ollama" else None
        )
        self._multimodal = None
        #: How long the server should keep the model resident.  Named plainly
        #: because it applies to ordinary chat too; the multimodal path had
        #: quietly owned the only copy of it.
        self._keep_alive = (keep_alive or "").strip() or None
        self._multimodal_keep_alive = keep_alive
        self._multimodal_timeout_s = float(timeout_s)
        self._multimodal_context_size = max(2048, int(multimodal_context_size))
        self._multimodal_max_tokens = max(64, min(512, int(multimodal_max_tokens)))
        self._multimodal_temperature = max(0.0, min(2.0, float(multimodal_temperature)))
        fallback = (ollama_fallback_model or "").strip()
        self._ollama_fallback_model = fallback or None
        self._auto_fallback_on_runner_crash = bool(auto_fallback_on_runner_crash)
        self._fallback_notice: str | None = None
        logger.info(
            "LLM backend: %s / %s (%s) reasoning_effort=%s num_ctx=%d",
            name, model, base_url, self._reasoning_effort or "(既定)", self._num_ctx,
        )

    @property
    def model(self) -> str:
        """The model currently used by this running backend."""
        return self._model

    @property
    def configured_model(self) -> str:
        """The model selected in settings, even after a runtime fallback."""
        return self._configured_model

    @property
    def fallback_notice(self) -> str | None:
        return self._fallback_notice

    def _is_ollama_fallback_failure(self, error: BaseException) -> bool:
        """Identify a runner crash or a configured model removed from Ollama."""
        if self.name != "ollama":
            return False
        text = str(error).lower()
        if any(signature in text for signature in (
            "llama-server process has terminated",
            "0xc0000409",
            "stack-based buffer",
            "ggml_assert",
        )):
            return True
        # Only switch while the configured primary model is active.  A typo
        # in the fallback model itself must remain visible to the user.
        return self._model == self._configured_model and (
            "model" in text and "not found" in text
        )

    def _activate_ollama_fallback(self, error: BaseException) -> bool:
        """Switch once after a confirmed runner crash without rewriting config."""
        fallback = self._ollama_fallback_model
        if (
            self.name != "ollama"
            or not self._auto_fallback_on_runner_crash
            or not fallback
            or fallback == self._model
            or not self._is_ollama_fallback_failure(error)
        ):
            return False
        failed_model = self._model
        self._model = fallback
        # The adapter captures the model at construction time.
        self._multimodal = None
        self._fallback_notice = (
            f"Ollama のモデル実行が異常終了したため、{failed_model} から "
            f"{fallback} へ一時的に切り替えました。"
        )
        logger.error(
            "Ollama runner crashed for model=%s; using runtime fallback=%s. error=%s",
            failed_model, fallback, error,
        )
        return True

    def _multimodal_client(self):
        if self.name != "ollama":
            raise RuntimeError(f"{self.name} does not support the Ollama multimodal adapter")
        if self._multimodal is None:
            from neuro_voice.llm.ollama_multimodal import OllamaMultimodalClient

            self._multimodal = OllamaMultimodalClient(
                self._base_url, self._model, timeout_s=self._multimodal_timeout_s,
                keep_alive=self._multimodal_keep_alive, retries=1,
            )
        return self._multimodal

    async def generate_with_media(
        self, messages: list[dict[str, Any]], media: list[MediaInput], *, request_id: str | None = None,
    ) -> AsyncIterator[str]:
        yielded = False
        try:
            async for token in self._multimodal_client().chat(
                messages, media, stream=True, request_id=request_id,
                options={
                    "num_ctx": self._multimodal_context_size,
                    "num_predict": self._multimodal_max_tokens,
                    "temperature": self._multimodal_temperature,
                },
                think=False,
            ):
                yielded = True
                yield token
        except Exception as error:
            # Retrying after emitted content would duplicate spoken text.
            if yielded or not self._activate_ollama_fallback(error):
                raise
            async for token in self._multimodal_client().chat(
                messages, media, stream=True, request_id=request_id,
                options={
                    "num_ctx": self._multimodal_context_size,
                    "num_predict": self._multimodal_max_tokens,
                    "temperature": self._multimodal_temperature,
                },
                think=False,
            ):
                yield token

    def cancel_request(self, request_id: str | None) -> None:
        if self._multimodal is not None:
            self._multimodal.cancel(request_id)

    def request_extra_body(self) -> dict[str, object]:
        """Fields the OpenAI SDK will not model, passed through verbatim.

        Split out so it can be tested without a server.  A previous version of
        this built the dict inline and referred to an attribute that did not
        exist; nothing caught it until every reply failed at runtime.

        * ``reasoning_effort`` — the SDK validates its enum client-side and
          "none" is outside it, so it goes raw.
        * ``keep_alive`` — measured: the configured ``10m`` never reached the
          server because it was only stored for the multimodal path, so Ollama
          used its 5 minute default and unloaded the model during any longer
          pause.  The first sentence afterwards paid a full reload (6.6-7.9s).
        * ``options`` — num_ctx / num_predict.  Measured to make no difference
          to latency; ``llm.send_ollama_options: false`` turns it off.
        """
        extra: dict[str, object] = {}
        if self._reasoning_effort:
            extra["reasoning_effort"] = self._reasoning_effort
        if not self._is_ollama:
            return extra
        if self._keep_alive:
            extra["keep_alive"] = self._keep_alive
        if self._send_options:
            options: dict[str, object] = {
                "num_ctx": self._num_ctx,
                "num_predict": self._max_tokens,
            }
            # 指定されたものだけ載せる。未指定の項目を既定値で埋めると、
            # モデル自身の推奨値を上書きしてしまう。
            if self._top_p is not None:
                options["top_p"] = self._top_p
            if self._top_k is not None:
                options["top_k"] = self._top_k
            if self._min_p is not None:
                options["min_p"] = self._min_p
            extra["options"] = options
        return extra

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        import time

        req_start = time.perf_counter()
        self.last_generation_metrics = {}
        extra_body = self.request_extra_body()

        async def create_stream():
            return await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                stream=True,
                extra_body=extra_body or None,
            )

        # Measured: first token ≈ 1114ms + 0.241ms × uncached tokens.  The
        # ~1114ms floor is there even when almost the whole prompt is cached,
        # so it is not prompt evaluation.  Splitting the wait tells us which
        # part of the round trip it actually is:
        #   接続   — until the server accepted the request and sent headers
        #   生成   — from headers to the first token
        # and ``同時実行`` shows whether this request was queued behind another
        # one on a server that processes them one at a time (D-017).
        concurrent = _inflight.enter()
        try:
            try:
                stream = await create_stream()
            except Exception as error:
                if not self._activate_ollama_fallback(error):
                    raise
                stream = await create_stream()
            stream_ready_ms = (time.perf_counter() - req_start) * 1000
        except BaseException:
            _inflight.leave()
            raise
        got_content = False
        saw_reasoning = False
        finish = None
        first_raw_ms: float | None = None   # 本文/思考問わず最初のトークン到達時間
        first_text_ms: float | None = None  # 本文(content)の最初のトークン到達時間
        provider_metrics: dict[str, float] = {}
        try:
            async for chunk in stream:
                extra = getattr(chunk, "model_extra", None) or {}
                for key, output_key in (("load_duration", "model_load_ms"),
                                        ("prompt_eval_duration", "prompt_eval_ms")):
                    raw = extra.get(key)
                    if isinstance(raw, (int, float)):
                        provider_metrics[output_key] = round(float(raw) / 1_000_000, 1)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                reasoning = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                )
                if (delta.content or reasoning) and first_raw_ms is None:
                    first_raw_ms = (time.perf_counter() - req_start) * 1000
                if delta.content:
                    if first_text_ms is None:
                        first_text_ms = (time.perf_counter() - req_start) * 1000
                    got_content = True
                    yield delta.content
                elif reasoning:
                    # 思考型モデルが本文とは別フィールドに推論を出す場合を検知
                    saw_reasoning = True
        finally:
            _inflight.leave()
            self.last_generation_metrics = {
                "provider_first_token_ms": None if first_text_ms is None else round(first_text_ms, 1),
                "provider_stream_ready_ms": round(stream_ready_ms, 1),
                "llm_queue_wait_ms": 0.0 if concurrent == 1 else None,
                "llm_queue_contended": concurrent > 1,
                "model_load_ms": provider_metrics.get("model_load_ms"),
                "prompt_eval_ms": provider_metrics.get("prompt_eval_ms"),
            }
        # 初回遅延の内訳を記録: 最初のトークンまで(=プロンプト評価+ロード)と、
        # 本文開始まで(=思考トークン込み)の差で原因を切り分けられる。
        logger.info(
            "LLM生成: 初トークン=%s ms / 本文開始=%s ms / 思考あり=%s / finish=%s",
            None if first_raw_ms is None else round(first_raw_ms),
            None if first_text_ms is None else round(first_text_ms),
            saw_reasoning, finish,
        )
        # Where the fixed ~1114ms actually goes.  「接続」 is everything before
        # the server started streaming; 「生成」 is prompt evaluation plus the
        # first token.  A large 接続 means the wait is queueing or model
        # loading, not the prompt.
        logger.info(
            "LLM内訳: 接続=%dms / 生成=%sms / 同時実行=%d / 待ち=%dms",
            round(stream_ready_ms),
            "-" if first_raw_ms is None else round(first_raw_ms - stream_ready_ms),
            concurrent,
            round(_inflight.last_wait_ms),
        )
        if finish == "length":
            # The reply was cut off mid-sentence.  Almost always the prompt has
            # filled num_ctx and left no room to answer, rather than the answer
            # genuinely being longer than max_tokens.
            logger.warning(
                "応答が途中で打ち切られました (finish_reason=length)。"
                "プロンプトが num_ctx を埋めて出力枠が残っていない可能性が高いです"
                " (num_ctx=%s / max_tokens=%s)。会話履歴の切り詰めか num_ctx の拡大を検討してください。",
                self._num_ctx, self._max_tokens,
            )
        if not got_content:
            logger.warning(
                "LLMが本文を返しませんでした (finish_reason=%s, 推論のみ=%s)。"
                "finish_reason=length なら max_tokens 不足、"
                "空/エラーなら VRAM 不足でモデルがロードしきれていない可能性が高いです。",
                finish, saw_reasoning,
            )

    async def load(self) -> bool:
        """Ollama の場合のみ、モデルを事前に VRAM へロードする (ウォームアップ)。

        prompt なしの /api/generate はロードのみ行う。初回応答の待ち時間を無くせる。
        クラウドAPI (Cerebras等) では何もしない。
        """
        if self.name != "ollama":
            return True
        root = self._base_url.removesuffix("/v1")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=300) as client:
                async def warmup() -> None:
                    r = await client.post(
                        f"{root}/api/generate",
                        json={"model": self._model},
                    )
                    if r.is_error:
                        # httpx's default error hides Ollama's runner crash
                        # text, which is needed to choose the safe fallback.
                        raise RuntimeError(
                            f"Ollama warmup HTTP {r.status_code}: {r.text[:1000]}"
                        )

                try:
                    await warmup()
                except Exception as error:
                    if not self._activate_ollama_fallback(error):
                        raise
                    await warmup()
            logger.info("Ollama モデルをロードしました: %s", self._model)
            return True
        except Exception as e:
            logger.warning("Ollama モデルのロードに失敗 (%s): %s", self._model, e)
            return False

    async def unload(self) -> None:
        """Ollama の場合のみ、モデルを VRAM からアンロードする。

        keep_alive=0 を指定した空リクエストを送ると即時解放される。
        クラウドAPI (Cerebras等) では何もしない。
        """
        if self.name != "ollama":
            return
        root = self._base_url.removesuffix("/v1")
        try:
            import httpx

            if self._multimodal is not None:
                await self._multimodal.unload()
                self._multimodal = None

            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{root}/api/generate",
                    json={"model": self._model, "keep_alive": 0},
                )
            logger.info("Ollama モデルをアンロードしました: %s", self._model)
        except Exception:
            logger.warning("Ollama モデルのアンロードに失敗 (%s)", self._model)
