"""AI本体とDiscord接続の状態機械 (純粋ロジック・スレッド安全)。

指示書10章・11章に対応。二重起動/二重参加/二重終了/join-leave競合などを
排他制御で防ぐ。ここにはI/Oを一切持たせず、遷移の妥当性判定だけを行う
(テストしやすくするため)。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AppState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class DiscordState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTING = "DISCONNECTING"
    ERROR = "ERROR"


# どの状態からその操作を「開始してよいか」を定義する。
_APP_START_FROM = {AppState.STOPPED, AppState.ERROR}
_APP_STOP_FROM = {AppState.RUNNING, AppState.ERROR, AppState.STARTING}
_DISCORD_JOIN_FROM = {DiscordState.DISCONNECTED, DiscordState.ERROR}
_DISCORD_LEAVE_FROM = {DiscordState.CONNECTED, DiscordState.ERROR}


class StateError(RuntimeError):
    """不正な状態遷移や競合を表す。API層で409へ写像する。"""


@dataclass
class LauncherState:
    """ランチャーが保持する状態。すべての変更は内部ロックで直列化する。"""

    app: AppState = AppState.STOPPED
    discord: DiscordState = DiscordState.DISCONNECTED
    app_pid: Optional[int] = None
    app_started_at: Optional[float] = None
    app_ready: bool = False
    guild_id: str = ""
    guild_name: str = ""
    channel_id: str = ""
    channel_name: str = ""
    operation_in_progress: bool = False
    current_operation: str = ""
    last_operation: str = ""
    last_error: Optional[str] = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---------- 排他つき操作ゲート ----------

    def begin_operation(self, name: str) -> None:
        """操作の排他ロックを取得する。既に別操作中なら StateError。"""
        with self._lock:
            if self.operation_in_progress:
                raise StateError(
                    f"別の操作を実行中です ({self.current_operation})"
                )
            self.operation_in_progress = True
            self.current_operation = name

    def end_operation(self, name: str, *, error: Optional[str] = None) -> None:
        with self._lock:
            self.operation_in_progress = False
            self.current_operation = ""
            self.last_operation = name
            self.last_error = error

    # ---------- App遷移 ----------

    def can_start_app(self) -> bool:
        with self._lock:
            return self.app in _APP_START_FROM

    def mark_app_starting(self, pid: int) -> None:
        with self._lock:
            if self.app not in _APP_START_FROM:
                raise StateError(f"AIアシスタントは既に{self.app.value}です")
            self.app = AppState.STARTING
            self.app_pid = pid
            self.app_started_at = time.time()
            self.app_ready = False
            self.last_error = None

    def mark_app_ready(self) -> None:
        with self._lock:
            self.app = AppState.RUNNING
            self.app_ready = True

    def mark_app_stopping(self) -> None:
        with self._lock:
            if self.app not in _APP_STOP_FROM:
                raise StateError(f"AIアシスタントは{self.app.value}のため停止できません")
            self.app = AppState.STOPPING

    def mark_app_stopped(self) -> None:
        with self._lock:
            self.app = AppState.STOPPED
            self.app_pid = None
            self.app_started_at = None
            self.app_ready = False
            self.discord = DiscordState.DISCONNECTED
            self.guild_id = self.guild_name = self.channel_id = self.channel_name = ""

    def mark_app_error(self, message: str) -> None:
        with self._lock:
            self.app = AppState.ERROR
            self.app_ready = False
            self.last_error = message
            # クラッシュ時にDiscordがCONNECTEDのまま残らないようにする (指示書25章)
            if self.discord in {DiscordState.CONNECTED, DiscordState.CONNECTING}:
                self.discord = DiscordState.ERROR

    # ---------- Discord遷移 ----------

    def can_join(self) -> bool:
        with self._lock:
            return self.app == AppState.RUNNING and self.discord in _DISCORD_JOIN_FROM

    def mark_joining(self) -> None:
        with self._lock:
            if self.app != AppState.RUNNING:
                raise StateError("AIアシスタントが起動していません")
            if self.discord not in _DISCORD_JOIN_FROM:
                raise StateError(f"Discordは既に{self.discord.value}です")
            self.discord = DiscordState.CONNECTING

    def mark_joined(self, *, guild_id: str, guild_name: str,
                    channel_id: str, channel_name: str) -> None:
        with self._lock:
            self.discord = DiscordState.CONNECTED
            self.guild_id, self.guild_name = guild_id, guild_name
            self.channel_id, self.channel_name = channel_id, channel_name

    def mark_leaving(self) -> None:
        with self._lock:
            if self.discord not in _DISCORD_LEAVE_FROM:
                raise StateError(f"Discordは{self.discord.value}のため退出できません")
            self.discord = DiscordState.DISCONNECTING

    def mark_disconnected(self) -> None:
        with self._lock:
            self.discord = DiscordState.DISCONNECTED
            self.guild_id = self.guild_name = self.channel_id = self.channel_name = ""

    def mark_discord_error(self, message: str) -> None:
        with self._lock:
            self.discord = DiscordState.ERROR
            self.last_error = message

    # ---------- スナップショット ----------

    def snapshot(self, *, include_pid: bool = False) -> dict:
        with self._lock:
            data = {
                "app_state": self.app.value,
                "app_ready": self.app_ready,
                "app_started_at": (
                    _iso(self.app_started_at) if self.app_started_at else None
                ),
                "discord_state": self.discord.value,
                "guild_id": self.guild_id or None,
                "guild_name": self.guild_name or None,
                "channel_id": self.channel_id or None,
                "channel_name": self.channel_name or None,
                "operation_in_progress": self.operation_in_progress,
                "current_operation": self.current_operation or None,
                "last_operation": self.last_operation or None,
                "last_error": self.last_error,
            }
            if include_pid:
                data["app_pid"] = self.app_pid
            return data


def _iso(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
