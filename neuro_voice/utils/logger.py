"""logging 初期化。コンソール + ローテーションファイル出力。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """ルートロガーと latency 専用ロガーを設定する。"""
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        path / "neuro_voice.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    latency = logging.getLogger("latency")
    latency.setLevel(logging.INFO)
    latency.propagate = False
    lat_handler = RotatingFileHandler(
        path / "latency.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    lat_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    latency.handlers.clear()
    latency.addHandler(lat_handler)

    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
