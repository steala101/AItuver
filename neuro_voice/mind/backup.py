"""人格・記憶データのバックアップ管理。

対象はペルソナ別の全データファイル (全ペルソナ分):
  personality_*.json / mind_*.db / dialogue_*.json / adaptive_*.db
  relationships_*.json / temporal_*.json / speakers_*.json

- 定期自動バックアップ (mind.backup.interval_minutes)
- 手動バックアップ (設定画面から)
- 復元 (復元前に安全バックアップを自動作成。反映にはアプリ再起動が必要)
- SQLite は書き込み途中のコピーを避けるため sqlite3 の online backup API を使う
- 内容が前回から変わっていない自動バックアップはスキップする
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ペルソナ毎の永続データ。この形式以外はバックアップ/復元の対象にしない
PERSONA_FILE_PATTERNS = (
    "personality_*.json",
    "mind_*.db",
    "dialogue_*.json",
    "adaptive_*.db",
    "relationships_*.json",
    "temporal_*.json",
    "speakers_*.json",
)
_SAFE_BACKUP_NAME = re.compile(r"^persona_backup_[0-9]{8}_[0-9]{6}_(auto|manual|exit|pre_restore)\.zip$")


class BackupManager:
    """人格データのバックアップ作成・世代管理・復元。スレッドセーフ。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        backup_dir: str | Path | None = None,
        interval_minutes: float = 60.0,
        keep_auto: int = 14,
        keep_manual: int = 20,
        save_hook: Callable[[], None] | None = None,
    ):
        self._data_dir = Path(data_dir)
        self._backup_dir = Path(backup_dir) if backup_dir else self._data_dir / "backups"
        self._interval_s = max(300.0, float(interval_minutes) * 60.0)
        self._keep_auto = max(1, int(keep_auto))
        self._keep_manual = max(1, int(keep_manual))
        self._save_hook = save_hook
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature: str | None = None

    @classmethod
    def from_config(cls, cfg, data_dir: str | Path,
                    save_hook: Callable[[], None] | None = None) -> "BackupManager":
        return cls(
            data_dir,
            backup_dir=cfg.get("mind.backup.dir") or None,
            interval_minutes=float(cfg.get("mind.backup.interval_minutes", 60)),
            keep_auto=int(cfg.get("mind.backup.keep_auto", 14)),
            keep_manual=int(cfg.get("mind.backup.keep_manual", 20)),
            save_hook=save_hook,
        )

    # ---------- 定期実行 ----------

    def start(self) -> None:
        """定期バックアップスレッドを開始する (多重起動は無視)。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="persona-backup", daemon=True
        )
        self._thread.start()
        logger.info("人格データの自動バックアップを開始 (間隔 %.0f 分)", self._interval_s / 60)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.create_backup(reason="auto")
            except Exception:
                # バックアップ失敗で会話本体を止めない (エラー分離)
                logger.exception("自動バックアップに失敗 (次回また試します)")

    # ---------- 作成 ----------

    def _target_files(self) -> list[Path]:
        files: list[Path] = []
        for pat in PERSONA_FILE_PATTERNS:
            files.extend(p for p in self._data_dir.glob(pat) if p.is_file())
        return sorted(set(files))

    def _signature(self, files: list[Path]) -> str:
        parts = []
        for p in files:
            try:
                st = p.stat()
                parts.append(f"{p.name}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                continue
        return "|".join(parts)

    def _copy_sqlite(self, src: Path, dst: Path) -> None:
        """書き込み途中でも壊れないスナップショットを online backup API で作る。"""
        with sqlite3.connect(str(src)) as conn, sqlite3.connect(str(dst)) as out:
            conn.backup(out)

    @staticmethod
    def _cleanup_sqlite_snapshot(path: Path) -> None:
        """Remove a temporary snapshot without turning a successful backup into an error.

        Windows may keep SQLite's file handle alive for a few scheduler ticks after
        ``Connection.close()``.  Retrying here is sufficient in normal cases.  If a
        different process truly owns the file, leaving this harmless temp file is
        safer than reporting the whole, already-written ZIP as failed.
        """
        for attempt in range(6):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt < 5:
                    time.sleep(0.05 * (2 ** attempt))
            except OSError:
                logger.warning("SQLiteバックアップ一時ファイルを削除できません: %s", path, exc_info=True)
                return
        logger.warning("SQLiteバックアップ一時ファイルは使用中のため後で削除します: %s", path)

    def create_backup(self, reason: str = "manual") -> dict[str, Any]:
        """バックアップを1つ作成する。返り値: {ok, file?, skipped?, message}。"""
        with self._lock:
            if self._save_hook is not None:
                try:
                    self._save_hook()
                except Exception:
                    logger.exception("バックアップ前の状態保存に失敗 (ファイル上の最新状態で続行)")
            files = self._target_files()
            if not files:
                return {"ok": False, "message": "バックアップ対象のデータがまだありません"}

            sig = self._signature(files)
            if reason == "auto" and sig == self._last_signature:
                return {"ok": True, "skipped": True, "message": "前回から変更なし"}

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"persona_backup_{stamp}_{reason}.zip"
            self._backup_dir.mkdir(parents=True, exist_ok=True)
            path = self._backup_dir / name
            tmp = path.with_suffix(".tmp")
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in files:
                        if f.suffix == ".db":
                            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as t:
                                snap = Path(t.name)
                            try:
                                self._copy_sqlite(f, snap)
                                zf.write(snap, f.name)
                            finally:
                                self._cleanup_sqlite_snapshot(snap)
                        else:
                            zf.write(f, f.name)
                    meta = {
                        "created": datetime.now().isoformat(timespec="seconds"),
                        "reason": reason,
                        "files": [f.name for f in files],
                    }
                    zf.writestr("backup_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
                tmp.replace(path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            self._last_signature = sig
            self._rotate()
            logger.info("人格データをバックアップ: %s (%d ファイル)", name, len(files))
            return {"ok": True, "file": name, "message": f"{len(files)}ファイルを保存しました"}

    def _rotate(self) -> None:
        """世代管理。自動系(auto/exit/pre_restore)と手動を別枠で保持する。"""
        try:
            all_backups = sorted(
                (p for p in self._backup_dir.glob("persona_backup_*.zip")
                 if _SAFE_BACKUP_NAME.match(p.name)),
                key=lambda p: p.name, reverse=True,
            )
            manual = [p for p in all_backups if p.name.endswith("_manual.zip")]
            auto = [p for p in all_backups if not p.name.endswith("_manual.zip")]
            for old in auto[self._keep_auto:] + manual[self._keep_manual:]:
                old.unlink(missing_ok=True)
                logger.info("古いバックアップを整理: %s", old.name)
        except Exception:
            logger.exception("バックアップの世代整理に失敗")

    # ---------- 一覧・復元 ----------

    def list_backups(self) -> list[dict[str, Any]]:
        if not self._backup_dir.exists():
            return []
        out = []
        for p in sorted(self._backup_dir.glob("persona_backup_*.zip"), reverse=True):
            if not _SAFE_BACKUP_NAME.match(p.name):
                continue
            m = re.match(r"persona_backup_([0-9]{8})_([0-9]{6})_(\w+)\.zip", p.name)
            label = ""
            if m:
                d, t = m.group(1), m.group(2)
                label = f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"
            out.append({
                "name": p.name,
                "time": label,
                "reason": m.group(3) if m else "",
                "size_kb": round(p.stat().st_size / 1024, 1),
            })
        return out

    def restore_backup(self, name: str) -> dict[str, Any]:
        """バックアップを data ディレクトリへ展開する。

        安全のため復元前に pre_restore バックアップを作成する。
        実行中の各エンジンはメモリ上の状態を持つため、反映にはアプリ再起動が必要。
        """
        if not _SAFE_BACKUP_NAME.match(name or ""):
            return {"ok": False, "message": "不正なバックアップ名です"}
        src = self._backup_dir / name
        if not src.exists():
            return {"ok": False, "message": "バックアップファイルが見つかりません"}
        # 現状を退避してから復元 (失敗時に戻せる)
        pre = self.create_backup(reason="pre_restore")
        if not pre.get("ok"):
            return {"ok": False, "message": "復元前の退避に失敗したため中止しました"}
        with self._lock:
            try:
                restored = 0
                with zipfile.ZipFile(src) as zf:
                    for info in zf.infolist():
                        fname = Path(info.filename).name  # zip内のパスは信用しない
                        if fname == "backup_meta.json":
                            continue
                        if not any(Path(fname).match(pat) for pat in PERSONA_FILE_PATTERNS):
                            continue
                        with zf.open(info) as f:
                            (self._data_dir / fname).write_bytes(f.read())
                        restored += 1
                self._last_signature = None
                logger.info("バックアップ %s から %d ファイルを復元", name, restored)
                return {
                    "ok": True,
                    "restored": restored,
                    "message": f"{restored}ファイルを復元しました。反映にはアプリの再起動が必要です",
                }
            except Exception:
                logger.exception("バックアップの復元に失敗")
                return {"ok": False, "message": "復元中にエラーが発生しました (ログ参照)"}
