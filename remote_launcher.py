"""遠隔起動・Discord接続管理ランチャーの起動スクリプト。

    python remote_launcher.py             # トレイ常駐 (既定。依存が無ければヘッドレス)
    pythonw remote_launcher.py            # コンソール無しでトレイ常駐
    python remote_launcher.py --console   # トレイを使わずコンソールで動かす

常駐させる方法 (タスクスケジューラ等) は README を参照。
このプロセスは軽量に保ち、AIアシスタント本体 (--discordモード) は
スマートフォンからの要求に応じて起動する。本体は自動起動しない。
"""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Windowsコンソールの日本語文字化け対策 (run.py と同様)。pythonw では stdout=None。
if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv

from neuro_voice.remote.api import LauncherApi
from neuro_voice.remote.launcher import (
    AuditLog, Controller, LauncherConfig, resolve_bind_host,
)


def _setup_logging(log_path: str, *, console: bool) -> None:
    """一般ログと監査ログを1つのファイルへ集約する (トレイのログ表示用)。"""
    root = logging.getLogger("remote_launcher")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console and sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main() -> int:
    load_dotenv()  # .env から REMOTE_LAUNCHER_* / AI_APP_* を読み込む
    use_console = "--console" in sys.argv

    cfg = LauncherConfig.from_env()
    _setup_logging(cfg.log_path, console=use_console)
    log = logging.getLogger("remote_launcher")

    if not cfg.enabled:
        log.error("REMOTE_LAUNCHER_ENABLED=true を設定してください。起動を中止します。")
        return 2
    if not cfg.admin_token or len(cfg.admin_token) < 32:
        log.error("REMOTE_LAUNCHER_ADMIN_TOKEN が未設定か短すぎます (32文字以上必須)。中止します。")
        return 2

    try:
        host = resolve_bind_host(cfg)
    except RuntimeError as e:
        # Tailscale未検出時にLANへ自動公開しない (安全側に倒す)
        log.error("バインド先を決定できません: %s", e)
        return 3

    # 監査ログは集約した remote_launcher ロガー経由で同じファイルへ出す
    audit = AuditLog(cfg.log_path, use_shared_logger=True)
    controller = Controller(cfg, audit)
    controller.reattach()  # ランチャー再起動時、生きている本体があれば再接続
    audit.record(op="launcher_start", host=host, port=cfg.port)

    api = LauncherApi(controller, cfg)
    log.info("遠隔ランチャーを起動します: http://%s:%d (Tailscale内のみ)", host, cfg.port)

    def _stop() -> None:
        with __import__("contextlib").suppress(Exception):
            api.stop()
        with __import__("contextlib").suppress(Exception):
            audit.record(op="launcher_stop")

    # トレイ常駐 (依存があり、--console でない場合)
    tray_enabled = not use_console
    if tray_enabled:
        from neuro_voice.remote.tray import LauncherTray, tray_available

        if tray_available():
            # APIサーバーは別スレッドで、トレイ+ウィンドウはメインスレッドで動かす
            threading.Thread(
                target=lambda: _serve_quiet(api, host, cfg.port, log),
                name="launcher-api", daemon=True,
            ).start()
            tray = LauncherTray(
                controller=controller, cfg=cfg, log_path=cfg.log_path,
                host=host, port=cfg.port, on_quit=_stop,
            )
            try:
                tray.run()  # ウィンドウを閉じる/終了メニューまでブロック
            except KeyboardInterrupt:
                pass
            finally:
                _stop()
            return 0
        log.warning("トレイ用の依存 (pystray/pillow/tk) が無いためヘッドレスで起動します "
                    "(pip install pystray pillow で常駐アイコンが使えます)")

    # フォールバック: 従来どおりコンソール/ヘッドレスで待受
    try:
        api.serve(host, cfg.port)
    except KeyboardInterrupt:
        log.info("停止要求を受信しました")
    finally:
        _stop()
    return 0


def _serve_quiet(api, host: str, port: int, log) -> None:
    try:
        api.serve(host, port)
    except Exception:
        log.exception("APIサーバーが停止しました")


if __name__ == "__main__":
    raise SystemExit(main())
