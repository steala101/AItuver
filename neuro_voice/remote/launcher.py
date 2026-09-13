"""常駐ランチャー本体。AI本体 (--discordモード) の起動・監視・終了を司る。

指示書3章 (軽量・別プロセス)、6章 (固定設定での起動)、7章 (Ready判定)、
11章 (PIDだけで判定しない)、12章 (順序終了→強制終了フォールバック)、
25章 (安定性) に対応する。ここではHTTP/認証は扱わず、制御ロジックに専念する
(HTTPは api.py)。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from neuro_voice.remote.control_client import ControlClient
from neuro_voice.remote.state import LauncherState, StateError
from neuro_voice.utils.config import persist_yaml_value

logger = logging.getLogger("remote_launcher")


# ---------- 設定 ----------

@dataclass
class LauncherConfig:
    enabled: bool
    host: str                  # バインド先 (Tailscale IPv4 または 明示的localhost)
    port: int
    admin_token: str
    session_timeout_minutes: int
    rate_limit: int
    log_path: str
    app_executable: str        # 例: python.exe
    app_working_directory: str
    app_entrypoint: str        # 例: run.py --discord
    app_control_port: int
    startup_timeout_s: int
    shutdown_timeout_s: int
    default_guild_id: str
    default_channel_id: str
    allow_localhost_fallback: bool
    session_file: str

    @classmethod
    def from_env(cls) -> "LauncherConfig":
        def _b(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

        def _i(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, "") or default)
            except ValueError:
                return default

        return cls(
            enabled=_b("REMOTE_LAUNCHER_ENABLED", False),
            host=os.environ.get("REMOTE_LAUNCHER_HOST", "auto").strip() or "auto",
            port=_i("REMOTE_LAUNCHER_PORT", 8765),
            admin_token=os.environ.get("REMOTE_LAUNCHER_ADMIN_TOKEN", "").strip(),
            session_timeout_minutes=_i("REMOTE_LAUNCHER_SESSION_TIMEOUT_MINUTES", 1440),
            rate_limit=_i("REMOTE_LAUNCHER_RATE_LIMIT", 30),
            log_path=os.environ.get("REMOTE_LAUNCHER_LOG_PATH", "logs/remote_launcher.log"),
            app_executable=os.environ.get("AI_APP_EXECUTABLE", sys.executable),
            app_working_directory=os.environ.get(
                "AI_APP_WORKING_DIRECTORY", str(Path.cwd()),
            ),
            app_entrypoint=os.environ.get("AI_APP_ENTRYPOINT", "run.py --discord"),
            app_control_port=_i("AI_APP_CONTROL_PORT", 8766),
            startup_timeout_s=_i("AI_APP_STARTUP_TIMEOUT_SECONDS", 60),
            shutdown_timeout_s=_i("AI_APP_SHUTDOWN_TIMEOUT_SECONDS", 30),
            default_guild_id=os.environ.get("DEFAULT_DISCORD_GUILD_ID", "").strip(),
            default_channel_id=os.environ.get("DEFAULT_DISCORD_CHANNEL_ID", "").strip(),
            allow_localhost_fallback=_b("REMOTE_LAUNCHER_ALLOW_LOCALHOST", False),
            session_file=os.environ.get(
                "REMOTE_LAUNCHER_SESSION_FILE", "logs/remote_launcher_session.json",
            ),
        )


# ---------- Tailscale IP 検出 ----------

def detect_tailscale_ipv4() -> Optional[str]:
    """PCのTailscale IPv4 (100.64.0.0/10) を取得する。失敗時 None。"""
    # 1) tailscale CLI があれば最優先
    for cmd in (["tailscale", "ip", "-4"], ["tailscale.exe", "ip", "-4"]):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    ip = line.strip()
                    if _is_cgnat(ip):
                        return ip
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            pass
    # 2) インターフェースを走査して CGNAT 帯のアドレスを探す
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_cgnat(ip):
                return ip
    except OSError:
        pass
    return None


def _is_cgnat(ip: str) -> bool:
    """Tailscale が使う 100.64.0.0/10 かどうか。"""
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return a == 100 and 64 <= b <= 127


def resolve_bind_host(cfg: LauncherConfig) -> str:
    """待ち受けIPを決める。取得できなければ例外 (LANへ自動公開しない)。"""
    if cfg.host and cfg.host != "auto":
        # 明示指定。localhost系か Tailscale帯のみ許可し、それ以外は拒否。
        if cfg.host in ("127.0.0.1", "::1", "localhost"):
            if not cfg.allow_localhost_fallback:
                raise RuntimeError(
                    "localhost バインドは REMOTE_LAUNCHER_ALLOW_LOCALHOST=true "
                    "を明示した場合のみ許可されます"
                )
            return cfg.host
        if not _is_cgnat(cfg.host):
            raise RuntimeError(
                f"REMOTE_LAUNCHER_HOST={cfg.host} はTailscaleアドレスではありません。"
                "0.0.0.0 やLAN IPへのバインドは禁止です"
            )
        return cfg.host
    ip = detect_tailscale_ipv4()
    if ip:
        return ip
    if cfg.allow_localhost_fallback:
        logger.warning("Tailscale IPを検出できないため localhost で起動します (開発用)")
        return "127.0.0.1"
    raise RuntimeError(
        "Tailscale IPv4 を検出できませんでした。Tailscaleを起動するか、"
        "開発時は REMOTE_LAUNCHER_ALLOW_LOCALHOST=true を設定してください"
    )


# ---------- 監査ログ ----------

class AuditLog:
    def __init__(self, path: str, *, use_shared_logger: bool = False):
        """監査ログ。

        use_shared_logger=True のときは remote_launcher ロガー配下へ流し、
        一般ログと同じファイル(単一ハンドラ)へ集約する(トレイのログ表示用・
        ローテーション競合を避ける)。False のときは従来どおり専用ハンドラを持つ。
        """
        self._logger = logging.getLogger("remote_launcher.audit")
        self._logger.setLevel(logging.INFO)
        if use_shared_logger:
            self._logger.propagate = True  # 親 (remote_launcher) の集約ハンドラへ
            return
        self._logger.propagate = False
        if not self._logger.handlers:
            from logging.handlers import RotatingFileHandler

            Path(path).parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=5, encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._logger.addHandler(handler)

    def record(self, **fields) -> None:
        # トークン等の秘匿情報は呼び出し側で除外済みとする
        self._logger.info("AUDIT " + json.dumps(fields, ensure_ascii=False))


# ---------- プロセス制御 ----------

class Controller:
    """AI本体プロセスのライフサイクルと状態を一元管理する。"""

    def __init__(self, cfg: LauncherConfig, audit: AuditLog):
        self._cfg = cfg
        self._audit = audit
        self.state = LauncherState()
        self._proc: Optional[subprocess.Popen] = None
        self._control: Optional[ControlClient] = None
        self._control_token = ""
        self._op_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()

    # ----- 起動 -----

    def _entry_args(self) -> list[str]:
        return [self._cfg.app_executable, *shlex.split(self._cfg.app_entrypoint)]

    def start_app(self, source_ip: str = "") -> dict:
        with self._op_lock:
            if not self.state.can_start_app():
                if self.state.app.value == "RUNNING":
                    return {"ok": True, "message": "AIアシスタントは既に起動しています",
                            "idempotent": True}
                raise StateError(f"AIアシスタントは{self.state.app.value}のため起動できません")
            self.state.begin_operation("start_app")
        started = time.time()
        try:
            token = secrets.token_urlsafe(32)
            env = dict(os.environ)
            env["AI_APP_CONTROL_TOKEN"] = token           # コマンドライン非経由
            env["AI_APP_CONTROL_PORT"] = str(self._cfg.app_control_port)
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                # コンソール無し(pythonw)で起動された場合、子プロセスに無効な
                # 標準出力ハンドルを継承させない。継承すると子の print() が
                # OSError [Errno 22] で落ちる(応答が無音になる原因だった)。
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # AI本体はログをファイルへ書くので、標準出力/エラーは捨ててよい。
            # DEVNULL は常に有効なハンドルなので、子の print() がクラッシュしない。
            proc = subprocess.Popen(
                self._entry_args(),
                cwd=self._cfg.app_working_directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._proc = proc
            self._control_token = token
            self._control = ControlClient(token, port=self._cfg.app_control_port)
            self.state.mark_app_starting(proc.pid)
            self._save_session()
            ready = self._wait_ready(proc)
            if not ready:
                self._audit.record(op="start_app", result="timeout",
                                   duration_s=round(time.time() - started, 1),
                                   source=source_ip)
                self._force_stop(proc)
                self.state.mark_app_error("起動がタイムアウトしました")
                return {"ok": False, "message": "AIアシスタントの起動がタイムアウトしました"}
            self.state.mark_app_ready()
            self._start_monitor()
            self._audit.record(op="start_app", result="ok", pid=proc.pid,
                               duration_s=round(time.time() - started, 1), source=source_ip)
            return {"ok": True, "message": "AIアシスタントを起動しました"}
        except Exception as e:
            logger.exception("AI本体の起動に失敗")
            self.state.mark_app_error(str(e))
            self._audit.record(op="start_app", result="error", source=source_ip)
            return {"ok": False, "message": "AIアシスタントの起動に失敗しました"}
        finally:
            self.state.end_operation("start_app", error=self.state.last_error)

    def _wait_ready(self, proc: subprocess.Popen) -> bool:
        deadline = time.time() + self._cfg.startup_timeout_s
        while time.time() < deadline:
            if proc.poll() is not None:
                return False  # 起動途中でクラッシュ
            health = self._control.health(timeout=2.0) if self._control else None
            if health and health.get("ready"):
                return True
            time.sleep(1.0)
        return False

    # ----- 終了 -----

    def stop_app(self, source_ip: str = "") -> dict:
        with self._op_lock:
            if self.state.app.value == "STOPPED":
                return {"ok": True, "message": "AIアシスタントは既に停止しています",
                        "idempotent": True}
            self.state.begin_operation("stop_app")
        started = time.time()
        forced = False
        try:
            self.state.mark_app_stopping()
            self._monitor_stop.set()
            # 1) Discord参加中なら退出 → 2) 本体へ正常終了要求
            if self._control is not None:
                try:
                    self._control.leave()
                except Exception:
                    pass
                try:
                    self._control.shutdown()
                except Exception:
                    pass
            proc = self._proc
            if proc is not None:
                forced = not self._graceful_wait(proc)
                if forced:
                    self._force_stop(proc)
            self.state.mark_app_stopped()
            self._clear_session()
            self._audit.record(op="stop_app", result="ok", forced=forced,
                               duration_s=round(time.time() - started, 1), source=source_ip)
            return {"ok": True, "message": "AIアシスタントを停止しました", "forced": forced}
        except Exception as e:
            logger.exception("AI本体の停止に失敗")
            self.state.mark_app_error(str(e))
            self._audit.record(op="stop_app", result="error", forced=forced, source=source_ip)
            return {"ok": False, "message": "AIアシスタントを正常終了できませんでした"}
        finally:
            self._proc = None
            self._control = None
            self.state.end_operation("stop_app", error=self.state.last_error)

    def _graceful_wait(self, proc: subprocess.Popen) -> bool:
        try:
            proc.wait(timeout=self._cfg.shutdown_timeout_s)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _force_stop(self, proc: subprocess.Popen) -> None:
        """段階的強制終了 (terminate → 短時間待機 → kill)。"""
        with __import__("contextlib").suppress(Exception):
            proc.terminate()
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        with __import__("contextlib").suppress(Exception):
            proc.kill()
        with __import__("contextlib").suppress(Exception):
            proc.wait(timeout=5)

    # ----- Discord -----

    def join(self, channel_id: str, source_ip: str = "") -> dict:
        with self._op_lock:
            if self.state.app.value != "RUNNING" or self._control is None:
                raise StateError("AIアシスタントが起動していません")
            self.state.begin_operation("join")
        try:
            self.state.mark_joining()
            result = self._control.join(channel_id or None)
            if result.get("ok"):
                self.state.mark_joined(
                    guild_id=str(result.get("guild_id") or ""),
                    guild_name=str(result.get("guild_name") or ""),
                    channel_id=str(result.get("channel_id") or ""),
                    channel_name=str(result.get("channel_name") or ""),
                )
            else:
                self.state.mark_discord_error(str(result.get("message") or "join失敗"))
            self._audit.record(op="join", result="ok" if result.get("ok") else "fail",
                               channel_id=channel_id, source=source_ip)
            return result
        finally:
            self.state.end_operation("join", error=self.state.last_error)

    def leave(self, source_ip: str = "") -> dict:
        with self._op_lock:
            if self.state.discord.value == "DISCONNECTED":
                return {"ok": True, "message": "既に退出しています", "idempotent": True}
            if self._control is None:
                raise StateError("AIアシスタントが起動していません")
            self.state.begin_operation("leave")
        try:
            self.state.mark_leaving()
            result = self._control.leave()
            self.state.mark_disconnected()
            self._audit.record(op="leave", result="ok", source=source_ip)
            return result
        finally:
            self.state.end_operation("leave", error=self.state.last_error)

    # ----- ワンタッチ -----

    def start_and_join(self, channel_id: str, source_ip: str = "") -> dict:
        if self.state.app.value != "RUNNING":
            r = self.start_app(source_ip)
            if not r.get("ok"):
                return r
        if self.state.discord.value == "CONNECTED" and \
                self.state.channel_id == str(channel_id):
            return {"ok": True, "message": "既に参加済みです", "idempotent": True}
        return self.join(channel_id, source_ip)

    def leave_and_stop(self, source_ip: str = "") -> dict:
        if self.state.discord.value in ("CONNECTED", "ERROR"):
            self.leave(source_ip)
        return self.stop_app(source_ip)

    def channels(self) -> dict:
        if self._control is None or self.state.app.value != "RUNNING":
            return {"ok": False, "message": "先にAIアシスタントを起動してください",
                    "channels": []}
        try:
            return {"ok": True, **self._control.channels()}
        except Exception:
            return {"ok": False, "message": "チャンネル一覧を取得できません", "channels": []}

    # ----- ペルソナ / 話者 / こころ -----

    def _require_control(self, empty: dict) -> dict | None:
        if self._control is None or self.state.app.value != "RUNNING":
            return {"ok": False, "message": "先にAIアシスタントを起動してください", **empty}
        return None

    def personas(self) -> dict:
        err = self._require_control({"personas": []})
        if err:
            return err
        try:
            return {"ok": True, **self._control.personas()}
        except Exception:
            return {"ok": False, "message": "ペルソナ一覧を取得できません", "personas": []}

    def set_persona(self, key: str, source_ip: str = "") -> dict:
        err = self._require_control({})
        if err:
            return err
        result = self._control.set_persona(key)
        self._audit.record(op="set_persona", result="ok" if result.get("ok") else "fail",
                           persona=key, source=source_ip)
        return result

    def speakers(self) -> dict:
        err = self._require_control({"speakers": []})
        if err:
            return err
        try:
            return {"ok": True, **self._control.speakers()}
        except Exception:
            return {"ok": False, "message": "話者一覧を取得できません", "speakers": []}

    def set_speaker(self, speaker_id: str, source_ip: str = "") -> dict:
        err = self._require_control({})
        if err:
            return err
        result = self._control.set_speaker(speaker_id)
        self._audit.record(op="set_speaker", result="ok" if result.get("ok") else "fail",
                           source=source_ip)
        return result

    def mind_status(self) -> dict:
        err = self._require_control({"enabled": False})
        if err:
            return err
        try:
            return self._control.mind()
        except Exception:
            return {"enabled": False, "error": "こころステータスを取得できません"}

    # ----- STT / TTS 設定 -----
    # AI本体が起動中なら制御チャネルでライブ切替(実稼働デバイスも取れる)。
    # 未起動なら config.yaml を直接読み書きして「起動時にどちらを使うか」を
    # 選べるようにする。PWAはどちらの状態でも選択に色が付く。

    @property
    def _config_path(self) -> Path:
        override = os.environ.get("AI_APP_CONFIG_PATH", "").strip()
        if override:
            return Path(override)
        return Path(self._cfg.app_working_directory) / "config" / "config.yaml"

    def _app_running(self) -> bool:
        return self._control is not None and self.state.app.value == "RUNNING"

    def _read_config_value(self, dotted: str, default):
        """config.yaml から1値だけ安全に読む (未起動時の選択表示用)。"""
        try:
            import yaml

            with open(self._config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return default
        node = data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _persist_config_value(self, dotted: str, value) -> bool:
        return persist_yaml_value(self._config_path, dotted, value)

    def stt_status(self) -> dict:
        if self._app_running():
            try:
                return {"ok": True, **self._control.stt_status()}
            except Exception:
                return {"ok": False, "message": "音声認識の状態を取得できません"}
        # 未起動: config の選択値を「起動時の設定」として返す
        dev = str(self._read_config_value("stt.device", "cuda") or "cuda").lower()
        return {"ok": True, "offline": True, "requested": dev, "device": dev,
                "on_gpu": dev.startswith("cuda"), "fell_back": False,
                "model": "", "compute_type": str(self._read_config_value("stt.compute_type", "") or "")}

    def set_stt_device(self, device: str, source_ip: str = "") -> dict:
        device = str(device or "").strip().lower()
        if self._app_running():
            try:
                result = self._control.set_stt_device(device)
            except Exception:
                return {"ok": False, "message": "音声認識の切替に失敗しました (応答なし)"}
        else:
            ok = self._persist_config_value("stt.device", device)
            label = "GPU" if device.startswith("cuda") else "CPU"
            result = {"ok": ok, "offline": True, "requested": device, "device": device,
                      "on_gpu": device.startswith("cuda"), "fell_back": False,
                      "message": f"起動時に {label} で音声認識を開始します" if ok
                                 else "設定の保存に失敗しました"}
        self._audit.record(op="set_stt_device", result="ok" if result.get("ok") else "fail",
                           device=device, running=self._app_running(), source=source_ip)
        return result

    def tts_status(self) -> dict:
        if self._app_running():
            try:
                return {"ok": True, **self._control.tts_status()}
            except Exception:
                return {"ok": False, "message": "読み上げ音声の状態を取得できません"}
        eng = str(self._read_config_value("audio_output.backend",
                  self._read_config_value("tts.backend", "voicevox")) or "voicevox").lower()
        try:
            vol = float(self._read_config_value("tts.output_volume", 1.0) or 1.0)
        except (TypeError, ValueError):
            vol = 1.0
        device = ""
        if eng == "style_bert_vits2":
            device = str(self._read_config_value("tts.style_bert_vits2.device", "") or "").lower()
        return {"ok": True, "offline": True, "engine": eng, "volume": round(vol, 3),
                "device": device, "on_gpu": device.startswith("cuda")}

    def runtime_info(self) -> dict:
        """STT/TTS/LLM が今どこ(GPU/CPU)で動くかをまとめて返す。
        起動中は本体の実稼働値、未起動時は config.yaml の設定値。"""
        if self._app_running():
            try:
                return {"ok": True, **self._control.runtime_info()}
            except Exception:
                return {"ok": False, "message": "実行情報を取得できません"}
        backend = str(self._read_config_value("llm.backend", "") or "")
        model = str(self._read_config_value(f"llm.backends.{backend}.model", "") or "")
        info = {"ok": True, "offline": True,
                "stt": self.stt_status(), "tts": self.tts_status(),
                "llm": {"backend": backend, "model": model}}
        # ランチャーもGPUと同じPC上なので、AI未起動でもVRAM使用量を出せる
        try:
            from neuro_voice.utils.gpu import gpu_memory

            gpu = gpu_memory()
            if gpu:
                info["gpu"] = gpu
        except Exception:
            pass
        return info

    def set_tts_volume(self, volume, source_ip: str = "") -> dict:
        try:
            value = max(0.0, min(2.0, float(volume)))
        except (TypeError, ValueError):
            return {"ok": False, "message": "音量の値が不正です"}
        if self._app_running():
            try:
                result = self._control.set_tts_volume(value)
            except Exception:
                return {"ok": False, "message": "音量の変更に失敗しました"}
        else:
            ok = self._persist_config_value("tts.output_volume", value)
            result = {"ok": ok, "offline": True, "volume": round(value, 3),
                      "message": f"起動時の音量を{int(round(value * 100))}%にしました" if ok
                                 else "設定の保存に失敗しました"}
        self._audit.record(op="set_tts_volume", result="ok" if result.get("ok") else "fail",
                           running=self._app_running(), source=source_ip)
        return result

    def set_tts_engine(self, engine: str, source_ip: str = "") -> dict:
        aliases = {"sbv2": "style_bert_vits2"}
        engine = aliases.get(str(engine or "").strip().lower(), str(engine or "").strip().lower())
        if self._app_running():
            try:
                result = self._control.set_tts_engine(engine)
            except Exception:
                return {"ok": False, "message": "読み上げ音声の切替に失敗しました (応答なし)"}
        else:
            ok = self._persist_config_value("audio_output.backend", engine)
            name = "Style-Bert-VITS2" if engine == "style_bert_vits2" else "VOICEVOX"
            result = {"ok": ok, "offline": True, "engine": engine,
                      "message": f"起動時に {name} で読み上げます" if ok
                                 else "設定の保存に失敗しました"}
        self._audit.record(op="set_tts_engine", result="ok" if result.get("ok") else "fail",
                           engine=engine, running=self._app_running(), source=source_ip)
        return result

    # ----- 監視 -----

    def _start_monitor(self) -> None:
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="app-monitor", daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(3.0):
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is not None:
                # 予期しない終了 (クラッシュ)。自動再起動はしない (指示書25章)。
                if self.state.app.value not in ("STOPPING", "STOPPED"):
                    logger.warning("AI本体が予期せず終了しました (code=%s)", proc.returncode)
                    self.state.mark_app_error("AIアシスタントが予期せず終了しました")
                    self._clear_session()
                    self._audit.record(op="monitor", result="app_crashed",
                                       code=proc.returncode)
                return

    # ----- セッション永続化 (ランチャー再起動後の再接続用) -----

    def _save_session(self) -> None:
        with __import__("contextlib").suppress(Exception):
            path = Path(self._cfg.session_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "pid": self.state.app_pid,
                "control_port": self._cfg.app_control_port,
                "control_token": self._control_token,
                "started_at": self.state.app_started_at,
            }), encoding="utf-8")

    def _clear_session(self) -> None:
        with __import__("contextlib").suppress(Exception):
            Path(self._cfg.session_file).unlink(missing_ok=True)

    def reattach(self) -> None:
        """ランチャー再起動時、生きているAI本体があれば安全に再接続する。"""
        path = Path(self._cfg.session_file)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        token = str(data.get("control_token") or "")
        port = int(data.get("control_port") or self._cfg.app_control_port)
        if not token:
            return
        client = ControlClient(token, port=port)
        health = client.health(timeout=3.0)
        # 正しいトークンで応答した場合のみ正当なAI本体と確認できる。
        if health and health.get("ready"):
            self._control = client
            self._control_token = token
            self.state.app_pid = data.get("pid")
            self.state.app_started_at = data.get("started_at")
            self.state.mark_app_ready()
            vs = client.status()
            if vs.get("in_voice"):
                self.state.mark_joined(
                    guild_id=str(vs.get("guild_id") or ""),
                    guild_name=str(vs.get("guild_name") or ""),
                    channel_id=str(vs.get("channel_id") or ""),
                    channel_name=str(vs.get("channel_name") or ""),
                )
            logger.info("既存のAI本体へ再接続しました (pid=%s)", data.get("pid"))
        else:
            # 確認できないプロセスは勝手に終了しない (指示書11章)
            logger.info("セッション情報はあるが応答なし。不明状態として扱います")
            self._clear_session()
