"""BackupManager (人格データのバックアップ/復元) の動作テスト。"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

try:
    from neuro_voice.mind.backup import BackupManager
except ImportError:  # Python 3.10環境ではパッケージ初期化を経由せず直接ロード
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "nv_backup", Path(__file__).resolve().parents[1] / "neuro_voice" / "mind" / "backup.py"
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    BackupManager = _mod.BackupManager


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    (d / "personality_neru.json").write_text(
        json.dumps({"traits": {"curiosity": 0.9}}), encoding="utf-8"
    )
    (d / "relationships_neru.json").write_text("{}", encoding="utf-8")
    with sqlite3.connect(d / "mind_neru.db") as conn:
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT)")
        conn.execute("INSERT INTO memories (text) VALUES ('テスト記憶')")
        conn.commit()
    # 対象外ファイルはバックアップに含まれないこと
    (d / "minecraft_knowledge.json").write_text("{}", encoding="utf-8")
    return d


def _mgr(data_dir: Path, **kw) -> BackupManager:
    kw.setdefault("interval_minutes", 60)
    return BackupManager(data_dir, **kw)


def test_manual_backup_contains_persona_files_only(data_dir: Path):
    r = _mgr(data_dir).create_backup(reason="manual")
    assert r["ok"] and not r.get("skipped")
    path = data_dir / "backups" / r["file"]
    assert path.exists()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    assert {"personality_neru.json", "relationships_neru.json",
            "mind_neru.db", "backup_meta.json"} == names


def test_auto_backup_skips_when_unchanged(data_dir: Path):
    m = _mgr(data_dir)
    assert m.create_backup(reason="auto")["ok"]
    r2 = m.create_backup(reason="auto")
    assert r2["ok"] and r2.get("skipped")
    # 変更があれば再びバックアップされる
    (data_dir / "personality_neru.json").write_text("{\"x\": 1}", encoding="utf-8")
    r3 = m.create_backup(reason="auto")
    assert r3["ok"] and not r3.get("skipped")


def test_save_hook_called_before_backup(data_dir: Path):
    called = []
    m = _mgr(data_dir, save_hook=lambda: called.append(1))
    m.create_backup(reason="manual")
    assert called


def test_rotation_keeps_manual_separately(data_dir: Path, monkeypatch):
    m = _mgr(data_dir, keep_auto=2, keep_manual=2)
    stamps = iter(range(100))

    class _FakeDT:
        @staticmethod
        def now():
            import datetime as dt
            return dt.datetime(2026, 7, 19, 0, 0, next(stamps))

    import sys

    monkeypatch.setattr(sys.modules[BackupManager.__module__], "datetime", _FakeDT)
    for i in range(4):
        (data_dir / "personality_neru.json").write_text(f"{{\"i\": {i}}}", encoding="utf-8")
        m.create_backup(reason="auto")
    for i in range(4, 7):
        (data_dir / "personality_neru.json").write_text(f"{{\"i\": {i}}}", encoding="utf-8")
        m.create_backup(reason="manual")
    backups = m.list_backups()
    autos = [b for b in backups if b["reason"] == "auto"]
    manuals = [b for b in backups if b["reason"] == "manual"]
    assert len(autos) == 2 and len(manuals) == 2


def test_restore_roundtrip_with_pre_restore_safety(data_dir: Path):
    m = _mgr(data_dir)
    r = m.create_backup(reason="manual")
    # 破壊的変更を加えてから復元
    (data_dir / "personality_neru.json").write_text("{\"broken\": true}", encoding="utf-8")
    res = m.restore_backup(r["file"])
    assert res["ok"] and res["restored"] == 3
    restored = json.loads((data_dir / "personality_neru.json").read_text(encoding="utf-8"))
    assert restored == {"traits": {"curiosity": 0.9}}
    # 復元前退避が作られている
    reasons = {b["reason"] for b in m.list_backups()}
    assert "pre_restore" in reasons
    # DBも壊れていない
    with sqlite3.connect(data_dir / "mind_neru.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_restore_rejects_bad_names(data_dir: Path):
    m = _mgr(data_dir)
    assert not m.restore_backup("../../etc/passwd")["ok"]
    assert not m.restore_backup("persona_backup_20260719_000000_manual.zip")["ok"]  # 存在しない
