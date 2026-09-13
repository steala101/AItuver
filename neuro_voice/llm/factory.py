"""設定から LLM バックエンドを生成するファクトリ。"""
from __future__ import annotations

from neuro_voice.llm.base import LLMBackend
from neuro_voice.llm.openai_compat import OpenAICompatBackend
from neuro_voice.utils.config import Config


def build_llm(cfg: Config) -> LLMBackend:
    """llm.backend の値に応じたバックエンドを返す。"""
    name = cfg.get("llm.backend", "cerebras")
    backends = cfg.section("llm.backends")
    if name not in backends:
        raise ValueError(f"未定義のLLMバックエンド: {name}")
    b = backends[name]
    if not b.get("api_key") and name != "ollama":
        raise ValueError(f"{name} の api_key が未設定です (.env を確認してください)")
    return OpenAICompatBackend(
        name=name,
        base_url=b["base_url"],
        api_key=b.get("api_key", ""),
        model=b["model"],
        temperature=float(cfg.get("llm.temperature", 0.7)),
        max_tokens=int(cfg.get("llm.max_tokens", 512)),
        reasoning_effort=cfg.get("llm.reasoning_effort"),
        keep_alive=cfg.get("llm.keep_alive"),
        timeout_s=float(cfg.get("llm.timeout_sec", 45)),
        multimodal_context_size=int(cfg.get("vision.reactive.context_size", 4096)),
        multimodal_max_tokens=int(cfg.get("vision.reactive.max_output_tokens", 220)),
        multimodal_temperature=float(cfg.get("vision.reactive.temperature", .65)),
        ollama_fallback_model=cfg.get("llm.ollama_fallback_model"),
        auto_fallback_on_runner_crash=bool(
            cfg.get("llm.auto_fallback_on_runner_crash", True)
        ),
        num_ctx=int(cfg.get("llm.num_ctx", 4096)),
        send_ollama_options=bool(cfg.get("llm.send_ollama_options", True)),
        # 未設定なら None のまま送らない。モデル自身の推奨値を活かす。
        top_p=_optional_float(cfg, "llm.top_p"),
        top_k=_optional_int(cfg, "llm.top_k"),
        min_p=_optional_float(cfg, "llm.min_p"),
    )


def _optional_float(cfg: Config, key: str) -> float | None:
    value = cfg.get(key)
    return None if value is None or value == "" else float(value)


def _optional_int(cfg: Config, key: str) -> int | None:
    value = cfg.get(key)
    return None if value is None or value == "" else int(value)



