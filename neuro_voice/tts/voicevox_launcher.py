"""VOICEVOX エンジンの自動起動 (Windows)。

TTS が voicevox のとき、エンジンが未起動なら CLI (run.exe) を
バックグラウンドで起動して疎通が取れるまで待つ。
自分で起動したプロセスはアプリ終了時に terminate する。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# 通常インストーラー版の既定パス候補 (新しい配置を先に探す)
_DEFAULT_CANDIDATES = [
    r"%LOCALAPPDATA%\Programs\VOICEVOX\vv-engine\run.exe",
    r"%LOCALAPPDATA%\Programs\VOICEVOX\run.exe",
    r"C:\Program Files\VOICEVOX\vv-engine\run.exe",
    r"C:\Program Files\VOICEVOX\run.exe",
]

_process: subprocess.Popen | None = None


def is_engine_alive(base_url: str, timeout: float = 2.0) -> bool:
    try:
        requests.get(f"{base_url.rstrip('/')}/version", timeout=timeout)
        return True
    except requests.RequestException:
        return False


def _resolve_engine_path(configured: str | None) -> Path | None:
    candidates = [configured] if configured else []
    candidates += _DEFAULT_CANDIDATES
    checked = []
    for c in candidates:
        if not c:
            continue
        p = Path(os.path.expandvars(str(c)))
        if p.exists():
            logger.info("VOICEVOX エンジンを検出: %s", p)
            return p
        checked.append(str(p))
    logger.warning("VOICEVOX エンジンが見つかりません。探した場所: %s", " / ".join(checked))
    return None


def ensure_engine(
    base_url: str,
    engine_path: str | None = None,
    wait_s: float = 60.0,
    on_status=None,
) -> bool:
    """エンジン未起動なら起動して疎通を待つ。成功で True。

    on_status: 進捗メッセージを受け取るコールバック (GUI通知用、省略可)。
    """
    global _process

    def _status(msg: str) -> None:
        logger.info(msg)
        if on_status is not None:
            try:
                on_status(msg)
            except Exception:
                pass

    if is_engine_alive(base_url):
        _status("VOICEVOX エンジンは起動済みです")
        return True

    path = _resolve_engine_path(engine_path)
    if path is None:
        _status(
            "VOICEVOX エンジンが見つかりません。"
            "config.yaml の tts.voicevox.engine_path を設定してください。"
        )
        return False

    _status(f"VOICEVOX エンジンを起動中... ({path.name})")
    try:
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        _process = subprocess.Popen(
            [str(path)],
            cwd=str(path.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:
        _status(f"VOICEVOX エンジンの起動に失敗: {e}")
        return False

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _process.poll() is not None:
            _status("VOICEVOX エンジンが直後に終了しました (ポート競合等を確認)")
            _process = None
            return False
        if is_engine_alive(base_url):
            _status("VOICEVOX エンジンの準備完了")
            return True
        time.sleep(1.0)
    _status(f"VOICEVOX エンジンの起動待ちがタイムアウトしました ({int(wait_s)}秒)")
    return False


def shutdown_engine() -> None:
    """自分で起動したエンジンのみ終了させる。"""
    global _process
    if _process is None:
        return
    try:
        _process.terminate()
        _process.wait(timeout=5)
        logger.info("VOICEVOX エンジンを終了しました")
    except Exception:
        logger.warning("VOICEVOX エンジンの終了に失敗 (手動で終了してください)")
    finally:
        _process = None
