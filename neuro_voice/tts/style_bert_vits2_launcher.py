"""Style-Bert-VITS2 サーバーの自動起動 (Windows)。

TTS が style_bert_vits2 のとき、サーバー (server_fastapi.py) が未起動なら
インストールフォルダの venv Python でバックグラウンド起動し、疎通が取れるまで待つ。
自分で起動したプロセスはアプリ終了時に terminate する (VOICEVOXと同じ方針)。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_process: subprocess.Popen | None = None


def is_server_alive(base_url: str, timeout: float = 3.0) -> bool:
    """モデル一覧が返る = 全モデルのロードが完了しリクエスト可能。"""
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models/info", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _resolve_install(configured: str | None) -> tuple[Path, Path] | None:
    """(server_fastapi.py のあるフォルダ, venvのpython) を解決する。"""
    if not configured:
        logger.warning(
            "Style-Bert-VITS2 のインストールパスが未設定です "
            "(tts.style_bert_vits2.path)"
        )
        return None
    root = Path(os.path.expandvars(str(configured)))
    if not root.is_absolute() and not root.exists():
        # 相対パスがCWDから見つからない場合は、アプリ本体のルートから探す
        app_root = Path(__file__).resolve().parents[2]
        if (app_root / root).exists():
            root = app_root / root
    if not (root / "server_fastapi.py").exists():
        # 「Style-Bert-VITS2-master」のような親フォルダが指定された場合は
        # 中の本体フォルダを自動で探す
        for child in ("Style-Bert-VITS2", "Style-Bert-VITS2-master"):
            candidate = root / child
            if (candidate / "server_fastapi.py").exists():
                logger.info("Style-Bert-VITS2 本体を検出: %s", candidate)
                root = candidate
                break
        else:
            logger.warning("server_fastapi.py が見つかりません: %s", root)
            return None
    candidates = [
        root / "venv" / "Scripts" / "python.exe",  # Windows venv
        root / "venv" / "bin" / "python",          # Linux/Mac venv
    ]
    for py in candidates:
        if py.exists():
            return root, py
    logger.warning(
        "Style-Bert-VITS2 の venv Python が見つかりません (%s)。"
        "Initialize.bat 実行済みか確認してください", root / "venv"
    )
    return None


def ensure_server(
    base_url: str,
    install_path: str | None = None,
    *,
    device: str = "cuda",
    wait_s: float = 120.0,
    on_status=None,
) -> bool:
    """サーバー未起動なら起動して疎通を待つ。成功で True。

    モデルとBERTを全ロードするため初回起動は数十秒〜かかる。
    """
    global _process

    def _status(msg: str) -> None:
        logger.info(msg)
        if on_status is not None:
            try:
                on_status(msg)
            except Exception:
                pass

    if is_server_alive(base_url):
        _status("Style-Bert-VITS2 サーバーは起動済みです")
        return True

    resolved = _resolve_install(install_path)
    if resolved is None:
        _status(
            "Style-Bert-VITS2 が見つかりません。"
            "config.yaml の tts.style_bert_vits2.path を確認してください。"
        )
        return False
    root, py = resolved

    cmd = [str(py), "server_fastapi.py"]
    if str(device).lower() == "cpu":
        cmd.append("--cpu")
    _status("Style-Bert-VITS2 サーバーを起動中... (モデル読込に数十秒かかります)")
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        _process = subprocess.Popen(
            cmd,
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:
        _status(f"Style-Bert-VITS2 サーバーの起動に失敗: {e}")
        return False

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _process.poll() is not None:
            _status(
                "Style-Bert-VITS2 サーバーが直後に終了しました "
                "(venvのtorch/GPU対応やポート競合を確認。Server.bat単体起動でエラー内容を確認できます)"
            )
            _process = None
            return False
        if is_server_alive(base_url):
            _status("Style-Bert-VITS2 サーバーの準備完了")
            return True
        time.sleep(1.5)
    _status(f"Style-Bert-VITS2 の起動待ちがタイムアウトしました ({int(wait_s)}秒)")
    return False


def shutdown_server() -> None:
    """自分で起動したサーバーのみ終了させる。"""
    global _process
    if _process is None:
        return
    try:
        _process.terminate()
        _process.wait(timeout=8)
        logger.info("Style-Bert-VITS2 サーバーを終了しました")
    except Exception:
        logger.warning("Style-Bert-VITS2 サーバーの終了に失敗 (手動で終了してください)")
    finally:
        _process = None
