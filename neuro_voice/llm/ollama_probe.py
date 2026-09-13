"""Find out what the fixed ~1100ms before the first token actually is.

Measured over 40 turns on this machine:

    初トークン ≈ 1106ms + 0.244ms × (キャッシュされなかったトークン数)

The slope is prompt evaluation and behaves sensibly.  The intercept does not:
a request with 464 uncached tokens — about 113ms of evaluation — still waited
1138ms, with ``同時実行=1`` and ``待ち=0ms``, so it is neither our own queueing
nor the prompt.

The OpenAI-compatible endpoint hides the numbers that would explain it.
Ollama's native ``/api/chat`` returns them:

* ``load_duration``        — bringing the model into memory
* ``prompt_eval_duration`` — evaluating the prompt
* ``eval_duration``        — producing tokens

So this probe asks the native endpoint directly, with a prompt small enough
that evaluation cannot account for anything, and reports where the time goes.

It runs the same tiny request four ways — with and without the per-request
``options`` we send, twice each — because passing ``options`` is the only
unusual thing in our requests and can make a server reconfigure its runner.
Two hypotheses, one run, no config editing and no restart cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Short enough that prompt evaluation is noise.
_PROMPT = "こんにちは"


def _ms(nanoseconds: Any) -> float:
    try:
        return round(float(nanoseconds) / 1_000_000, 1)
    except (TypeError, ValueError):
        return 0.0


@dataclass(slots=True)
class ProbeCall:
    """One tiny request, as the server described it."""

    with_options: bool
    #: Context size requested, so a sweep can show what ``load_duration``
    #: actually tracks.  0 means "whatever the server defaults to".
    num_ctx: int = 0
    keep_alive: str = ""
    round_trip_ms: float = 0.0
    load_ms: float = 0.0
    prompt_eval_ms: float = 0.0
    eval_ms: float = 0.0
    total_ms: float = 0.0
    prompt_tokens: int = 0
    error: str = ""

    @property
    def unexplained_ms(self) -> float:
        """Round trip minus everything the server admits to."""
        return round(
            max(0.0, self.round_trip_ms - self.load_ms - self.prompt_eval_ms - self.eval_ms),
            1,
        )

    @property
    def label(self) -> str:
        parts = [f"num_ctx={self.num_ctx or '既定'}"]
        if not self.with_options:
            parts.append("options無")
        if self.keep_alive:
            parts.append(f"keep_alive={self.keep_alive}")
        return " ".join(parts)

    def line(self) -> str:
        if self.error:
            return f"{self.label} エラー: {self.error}"
        return (
            f"{self.label:<34} 往復={self.round_trip_ms:>5.0f}ms "
            f"(準備={self.load_ms:>5.0f} / 評価={self.prompt_eval_ms:>4.0f}"
            f"[{self.prompt_tokens}tok] / 生成={self.eval_ms:>4.0f} / 説明なし={self.unexplained_ms:>4.0f})"
        )


@dataclass(slots=True)
class ProbeResult:
    loaded_models: list[str] = field(default_factory=list)
    #: Same listing after the probe, so a changed expiry shows whether the
    #: ``keep_alive`` we ask for is reaching the server at all.
    loaded_after: list[str] = field(default_factory=list)
    calls: list[ProbeCall] = field(default_factory=list)
    error: str = ""

    def _load_by_context(self) -> list[tuple[int, float]]:
        """Steady-state preparation time per context size.

        **Minimum, not mean.**  Changing ``num_ctx`` makes the server reload
        the model outright — measured at 6.6-7.9 seconds — so the first call
        at each size is a reload artefact of the sweep itself.  Averaging the
        two produced 3624 / 4088 / 3767ms, numbers that describe nothing.
        The second call at the same size is what the steady state costs.
        """
        by_size: dict[int, list[float]] = {}
        for call in self.calls:
            if call.error or not call.with_options or not call.num_ctx or call.keep_alive:
                continue
            by_size.setdefault(call.num_ctx, []).append(call.load_ms)
        return [
            (size, round(min(values), 1)) for size, values in sorted(by_size.items())
        ]

    @property
    def reload_ms(self) -> float:
        """Cost of an outright reload, seen when the context size changes."""
        spikes = [
            call.load_ms for call in self.calls
            if not call.error and call.load_ms >= 3000
        ]
        return round(min(spikes), 1) if spikes else 0.0

    @property
    def steady_load_ms(self) -> float:
        """Steady-state preparation at the context size actually configured.

        The largest size in the sweep is the one the application runs with, so
        that is the number the verdict has to judge — not the cheapest.
        """
        curve = self._load_by_context()
        return curve[-1][1] if curve else 0.0

    def _mean(self, *, with_options: bool, attribute: str) -> float:
        values = [
            getattr(call, attribute) for call in self.calls
            if call.with_options is with_options and not call.error
        ]
        return round(sum(values) / len(values), 1) if values else 0.0

    def verdict(self) -> str:
        """Plain-language reading of the numbers, or what is still unclear."""
        if self.error:
            return f"判定不能: {self.error}"
        warm = [call for call in self.calls if not call.error]
        if not warm:
            return "判定不能: 有効な計測が取れませんでした"
        # The steady state, not the reload spikes the sweep causes itself.
        load = self.steady_load_ms or max(call.load_ms for call in warm)
        unexplained = max(call.unexplained_ms for call in warm)
        spilled = [line for line in self.loaded_models if "溢れている" in line]
        reasons: list[str] = []

        if load >= 300 and spilled:
            reasons.append(
                f"モデル準備に毎回{load:.0f}ms。**モデルがVRAMに収まりきっていない**ため、"
                "リクエストごとに用意し直している。GPUを分け合っている他のもの"
                "（Whisper / Style-Bert-VITS2 / 映像認識）を減らすか、"
                "より小さい量子化にすると消える"
            )
        elif load >= 300:
            # The sweep is what tells these two apart.
            curve = self._load_by_context()
            if len(curve) >= 2:
                small, large = curve[0], curve[-1]
                growth = large[1] - small[1]
                if growth >= 150:
                    reasons.append(
                        f"モデル準備が num_ctx に比例している"
                        f"（{small[0]}→{small[1]:.0f}ms / {large[0]}→{large[1]:.0f}ms）。"
                        "**KVキャッシュを毎回確保し直している**。num_ctx を下げるか、"
                        "KVキャッシュの量子化（OLLAMA_KV_CACHE_TYPE=q8_0）で縮む"
                    )
                else:
                    reasons.append(
                        f"定常状態のモデル準備が毎回{load:.0f}msで、num_ctx を8倍にしても"
                        f"{large[1] - small[1]:.0f}msしか増えない"
                        f"（{small[0]}→{small[1]:.0f}ms / {large[0]}→{large[1]:.0f}ms）。"
                        "**KVキャッシュでもモデルの再ロードでもない**。"
                        "Ollama側のリクエストごとの固定処理なので、"
                        "サーバの版・設定（OLLAMA_NUM_PARALLEL 等）を見るか、"
                        "推論サーバ自体を替えるかの判断になる"
                    )
            else:
                reasons.append(f"モデル準備に毎回{load:.0f}ms。原因は未特定")
        if unexplained >= 300 and not reasons:
            reasons.append(
                f"サーバが説明していない時間が{unexplained:.0f}ms。"
                "待ち行列かスケジューラ側（OLLAMA_NUM_PARALLEL / 他モデルとの競合）"
            )
        expiry = self._keep_alive_note()
        if expiry:
            reasons.append(expiry)
        if not reasons:
            return (
                "小さなリクエストは速く返っています。固定費は会話リクエスト固有の"
                "条件（プロンプト長ではない何か）にあります"
            )
        return " / ".join(reasons)

    def _keep_alive_note(self) -> str:
        """Whether asking to stay resident changed anything."""
        before = next((line for line in self.loaded_models if "期限=" in line), "")
        after = next((line for line in self.loaded_after if "期限=" in line), "")
        if not before or not after:
            return ""
        if before.split("期限=")[-1] == after.split("期限=")[-1]:
            return "keep_alive を送っても常駐期限が変わらない（サーバ側で固定されている）"
        return ""

    def summary(self) -> str:
        lines = ["読み込み済みモデル:"]
        lines += [f"  {line}" for line in self.loaded_models] or ["  (なし)"]
        lines += [f"  {call.line()}" for call in self.calls]
        curve = self._load_by_context()
        if len(curve) >= 2:
            lines.append(
                "  num_ctx と準備時間(定常): "
                + " / ".join(f"{size}→{value:.0f}ms" for size, value in curve)
            )
        if self.reload_ms:
            lines.append(
                f"  ※ num_ctx を変えた直後だけ {self.reload_ms:.0f}ms かかる"
                "（モデルの再ロード。この掃引が自分で起こしたもので、通常の会話では起きない）"
            )
        for line in self.loaded_after:
            if "期限=" in line:
                lines.append(f"  計測後: {line}")
        lines.append(f"  判定: {self.verdict()}")
        return "\n".join(lines)


def _loaded_models(session, root: str, timeout: float) -> list[str]:
    """What is resident, and — decisively — whether it all fits in VRAM.

    ``load_duration`` was ~590ms on every request even with the model already
    listed as loaded and ``keep_alive=10m``.  A model that does not fit
    entirely in VRAM is prepared again for each request, so the two numbers
    ``/api/ps`` reports separately are what distinguish "resident" from
    "resident on the GPU".
    """
    try:
        response = session.get(f"{root}/api/ps", timeout=timeout)
        response.raise_for_status()
    except Exception as error:
        logger.debug("Ollama /api/ps の取得に失敗: %s", error)
        return []
    lines: list[str] = []
    for item in (response.json().get("models") or []):
        try:
            total = int(item.get("size", 0) or 0)
            in_vram = int(item.get("size_vram", 0) or 0)
        except (TypeError, ValueError):
            total = in_vram = 0
        mb = 1024 ** 2
        placement = "全部GPU"
        if total and in_vram < total:
            spilled = (total - in_vram) / mb
            share = in_vram / total
            placement = f"**CPUへ{spilled:.0f}MB溢れている** (GPU上は{share:.0%})"
        lines.append(
            f"{item.get('name')}: 合計{total / mb:.0f}MB / VRAM{in_vram / mb:.0f}MB "
            f"— {placement}"
            + (f" / 期限={item.get('expires_at')}" if item.get("expires_at") else "")
        )
    return lines


def _one_call(
    session, root: str, model: str, *, with_options: bool,
    num_ctx: int, max_tokens: int, timeout: float, keep_alive: str = "",
) -> ProbeCall:
    call = ProbeCall(
        with_options=with_options,
        num_ctx=int(num_ctx) if with_options else 0,
        keep_alive=str(keep_alive or ""),
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT}],
        "stream": False,
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive
    if with_options:
        payload["options"] = {"num_ctx": int(num_ctx), "num_predict": 8}
    else:
        payload["options"] = {"num_predict": 8}
    started = time.perf_counter()
    try:
        response = session.post(f"{root}/api/chat", json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except Exception as error:
        call.round_trip_ms = (time.perf_counter() - started) * 1000
        call.error = str(error)[:120]
        return call
    call.round_trip_ms = round((time.perf_counter() - started) * 1000, 1)
    call.load_ms = _ms(body.get("load_duration"))
    call.prompt_eval_ms = _ms(body.get("prompt_eval_duration"))
    call.eval_ms = _ms(body.get("eval_duration"))
    call.total_ms = _ms(body.get("total_duration"))
    try:
        call.prompt_tokens = int(body.get("prompt_eval_count") or 0)
    except (TypeError, ValueError):
        call.prompt_tokens = 0
    return call


def probe_ollama(
    base_url: str,
    model: str,
    *,
    num_ctx: int = 8192,
    max_tokens: int = 1024,
    timeout: float = 60.0,
    repeats: int = 2,
) -> ProbeResult:
    """Ask the server itself where the time goes.  Blocking; run in a thread."""
    try:
        import requests
    except Exception as error:      # pragma: no cover - requests is a dependency
        return ProbeResult(error=f"requests を読み込めません: {error}")

    root = str(base_url or "").rstrip("/").removesuffix("/v1")
    result = ProbeResult()
    session = requests.Session()
    try:
        result.loaded_models = _loaded_models(session, root, timeout=5.0)
        # Warm-up: the very first request after start-up is genuinely cold and
        # would otherwise dominate every number below.
        _one_call(
            session, root, model, with_options=True,
            num_ctx=num_ctx, max_tokens=max_tokens, timeout=timeout,
        )
        # A sweep, because the question is what the 600ms tracks.  Scaling with
        # the context size means it is KV cache allocation; staying flat means
        # it is the runner or the scheduler.
        # Two calls per size: the first pays for the reload that changing the
        # size triggers (~7s, measured), the second shows the steady state.
        # Only the second is meaningful, so both are kept and the report takes
        # the minimum rather than the mean.
        sizes = sorted({1024, 4096, int(num_ctx)})
        for size in sizes:
            for _ in range(max(2, int(repeats))):
                result.calls.append(_one_call(
                    session, root, model, with_options=True,
                    num_ctx=size, max_tokens=max_tokens, timeout=timeout,
                ))
        # Leave the server on the size the application actually uses, so the
        # probe does not make the first real turn pay for a reload.
        _one_call(
            session, root, model, with_options=True,
            num_ctx=num_ctx, max_tokens=max_tokens, timeout=timeout,
        )
        # One without options at all, as the control.
        result.calls.append(_one_call(
            session, root, model, with_options=False,
            num_ctx=num_ctx, max_tokens=max_tokens, timeout=timeout,
        ))
        # And one asking to stay resident, to see whether keep_alive is even
        # reaching the server: the expiry observed was Ollama's 5m default
        # while the configuration said 10m.
        result.calls.append(_one_call(
            session, root, model, with_options=True, keep_alive="30m",
            num_ctx=num_ctx, max_tokens=max_tokens, timeout=timeout,
        ))
        result.loaded_after = _loaded_models(session, root, timeout=5.0)
    except Exception as error:
        result.error = str(error)[:160]
    finally:
        session.close()
    return result
