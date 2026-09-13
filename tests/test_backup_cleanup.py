from __future__ import annotations

import unittest

from neuro_voice.mind.backup import BackupManager


class _LockedThenFree:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def unlink(self, *, missing_ok=False):
        self.calls += 1
        if self.calls <= self.failures:
            raise PermissionError("locked")

    def __str__(self):
        return "temporary.db"


class BackupCleanupTests(unittest.TestCase):
    def test_windows_lock_is_retried(self):
        snap = _LockedThenFree(2)
        BackupManager._cleanup_sqlite_snapshot(snap)  # type: ignore[arg-type]
        self.assertEqual(snap.calls, 3)

    def test_persistent_lock_does_not_raise_after_backup(self):
        snap = _LockedThenFree(99)
        BackupManager._cleanup_sqlite_snapshot(snap)  # type: ignore[arg-type]
        self.assertEqual(snap.calls, 6)


if __name__ == "__main__":
    unittest.main()
