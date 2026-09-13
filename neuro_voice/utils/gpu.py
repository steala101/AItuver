"""nvidia-smi でGPU全体のVRAM使用量を取得する小さなヘルパー。

AI本体(GPUを使う側)とランチャー(同じPC上で状態表示する側)の双方から使う。
vram_percent が高い(≳90%)と、WindowsがGPUメモリをRAMへ退避(WDDMページング)し、
音声合成・LLM生成が急に遅くなりCPUが跳ねる。STT+SBV2+LLMを同時にGPUへ載せて
VRAMを超えるとこれが起きる (各コンポーネント単体では起きない)。

Windowsでは nvidia-smi を素で呼ぶとコンソール窓が一瞬開くため、CREATE_NO_WINDOW
で非表示にする。さらに数秒キャッシュして、複数箇所からの頻繁な呼び出しをまとめる。
"""
from __future__ import annotations

import os
import subprocess
import time

_CACHE_TTL_S = 3.0
_cache: dict = {"t": 0.0, "v": None}

# Windowsでコンソール窓を出さないためのフラグ (他OSでは0)。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run_nvidia_smi() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2.0,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        first = out.stdout.strip().splitlines()[0]
        used, total, util = (int(x.strip()) for x in first.split(",")[:3])
    except (ValueError, IndexError):
        return None
    pct = int(round(used / total * 100)) if total else None
    return {
        "vram_used_mb": used,
        "vram_total_mb": total,
        "vram_percent": pct,
        "gpu_util": util,
        "vram_tight": pct is not None and pct >= 90,
    }


def gpu_memory() -> dict | None:
    """GPU0のVRAM使用量と使用率を返す (数秒キャッシュ)。取得できなければ None。"""
    now = time.monotonic()
    if now - _cache["t"] < _CACHE_TTL_S and _cache["v"] is not None:
        return _cache["v"]
    value = _run_nvidia_smi()
    if value is not None:
        _cache["t"] = now
        _cache["v"] = value
    return value
