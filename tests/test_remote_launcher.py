"""遠隔ランチャー (バックエンド) の単体テスト。

指示書26章の単体テスト項目に対応:
  認証成功/失敗、レート制限、不正なguild_id/channel_id、不正な状態遷移、
  二重起動/二重終了/二重参加/二重退出の防止、Tailscale IP検出とフォールバック。
"""
import unittest
from pathlib import Path

from neuro_voice.remote.state import (
    AppState, DiscordState, LauncherState, StateError,
)
from neuro_voice.remote import launcher as L
from neuro_voice.remote.api import RateLimiter, Sessions, _valid_id


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.s = LauncherState()

    def test_initial(self):
        self.assertEqual(self.s.app, AppState.STOPPED)
        self.assertEqual(self.s.discord, DiscordState.DISCONNECTED)

    def test_no_double_start(self):
        self.s.mark_app_starting(pid=100)
        self.s.mark_app_ready()
        self.assertFalse(self.s.can_start_app())
        with self.assertRaises(StateError):
            self.s.mark_app_starting(pid=101)

    def test_no_join_before_running(self):
        self.assertFalse(self.s.can_join())
        with self.assertRaises(StateError):
            self.s.mark_joining()

    def test_join_then_no_double_join(self):
        self.s.mark_app_starting(pid=1)
        self.s.mark_app_ready()
        self.assertTrue(self.s.can_join())
        self.s.mark_joining()
        self.s.mark_joined(guild_id="1", guild_name="g", channel_id="2", channel_name="c")
        with self.assertRaises(StateError):
            self.s.mark_joining()  # 二重参加防止

    def test_no_double_leave(self):
        with self.assertRaises(StateError):
            self.s.mark_leaving()  # DISCONNECTEDからは退出できない

    def test_operation_mutex(self):
        self.s.begin_operation("start_app")
        with self.assertRaises(StateError):
            self.s.begin_operation("stop_app")  # 別操作中は拒否
        self.s.end_operation("start_app")
        self.s.begin_operation("stop_app")  # 解放後はOK

    def test_crash_clears_discord_connected(self):
        self.s.mark_app_starting(pid=1)
        self.s.mark_app_ready()
        self.s.mark_joining()
        self.s.mark_joined(guild_id="1", guild_name="g", channel_id="2", channel_name="c")
        self.s.mark_app_error("crashed")
        # クラッシュ後にDiscordがCONNECTEDのまま残らない (指示書25章)
        self.assertEqual(self.s.discord, DiscordState.ERROR)

    def test_stopped_clears_fields(self):
        self.s.mark_app_starting(pid=5)
        self.s.mark_app_ready()
        self.s.mark_joining()
        self.s.mark_joined(guild_id="1", guild_name="g", channel_id="2", channel_name="c")
        self.s.mark_app_stopping()
        self.s.mark_app_stopped()
        snap = self.s.snapshot()
        self.assertEqual(snap["app_state"], "STOPPED")
        self.assertIsNone(snap["channel_id"])
        self.assertNotIn("app_pid", snap)  # PIDは既定で非公開

    def test_snapshot_pid_opt_in(self):
        self.s.mark_app_starting(pid=42)
        self.assertEqual(self.s.snapshot(include_pid=True)["app_pid"], 42)


class IdValidationTests(unittest.TestCase):
    def test_valid_and_invalid_ids(self):
        self.assertTrue(_valid_id("123456789012345678"))
        self.assertTrue(_valid_id(""))  # 空=自動選択は許容
        self.assertFalse(_valid_id("abc"))
        self.assertFalse(_valid_id("12; DROP TABLE"))
        self.assertFalse(_valid_id("9" * 40))  # 長すぎ


class AuthTests(unittest.TestCase):
    def test_session_lifecycle(self):
        sessions = Sessions(ttl_minutes=60)
        sid = sessions.create()
        self.assertTrue(sessions.valid(sid))
        self.assertFalse(sessions.valid("bogus"))
        sessions.drop(sid)
        self.assertFalse(sessions.valid(sid))

    def test_expired_session(self):
        sessions = Sessions(ttl_minutes=0)  # 即失効
        sid = sessions.create()
        self.assertFalse(sessions.valid(sid))


class RateLimitTests(unittest.TestCase):
    def test_blocks_after_limit(self):
        rl = RateLimiter(per_minute=3)
        self.assertTrue(rl.allow("1.2.3.4"))
        self.assertTrue(rl.allow("1.2.3.4"))
        self.assertTrue(rl.allow("1.2.3.4"))
        self.assertFalse(rl.allow("1.2.3.4"))  # 4回目は拒否
        self.assertTrue(rl.allow("5.6.7.8"))   # 別IPは独立


class TailscaleDetectionTests(unittest.TestCase):
    def test_cgnat_range(self):
        self.assertTrue(L._is_cgnat("100.101.102.103"))
        self.assertTrue(L._is_cgnat("100.64.0.1"))
        self.assertFalse(L._is_cgnat("192.168.1.10"))
        self.assertFalse(L._is_cgnat("100.128.0.1"))  # /10 の外
        self.assertFalse(L._is_cgnat("not.an.ip"))

    def test_resolve_refuses_lan(self):
        cfg = _fake_cfg(host="192.168.1.5")
        with self.assertRaises(RuntimeError):
            L.resolve_bind_host(cfg)

    def test_resolve_refuses_localhost_without_flag(self):
        cfg = _fake_cfg(host="127.0.0.1", allow_localhost_fallback=False)
        with self.assertRaises(RuntimeError):
            L.resolve_bind_host(cfg)

    def test_resolve_allows_localhost_with_flag(self):
        cfg = _fake_cfg(host="127.0.0.1", allow_localhost_fallback=True)
        self.assertEqual(L.resolve_bind_host(cfg), "127.0.0.1")

    def test_resolve_accepts_explicit_tailscale(self):
        cfg = _fake_cfg(host="100.90.80.70")
        self.assertEqual(L.resolve_bind_host(cfg), "100.90.80.70")


class WindowsSilentEntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]

    def test_vbs_hides_even_the_python_exe_fallback(self):
        script = (self.root / "RemoteLauncher.vbs").read_text(encoding="utf-8")
        self.assertIn(".venv\\Scripts\\pythonw.exe", script)
        self.assertIn(".venv\\Scripts\\python.exe", script)
        self.assertIn("shell.Run command, 0, False", script)

    def test_normal_batch_delegates_to_silent_entry(self):
        script = (self.root / "RemoteLauncher.bat").read_text(encoding="utf-8")
        self.assertIn("wscript.exe", script.lower())
        self.assertIn("RemoteLauncher.vbs", script)
        self.assertNotIn("set \"PY=python\"", script)

    def test_autostart_shortcut_targets_wscript_not_cmd(self):
        script = (self.root / "InstallAutoStart.bat").read_text(encoding="utf-8")
        self.assertIn("wscript.exe", script.lower())
        self.assertIn("RemoteLauncher.vbs", script)
        self.assertNotIn("RemoteLauncher.bat'", script)


def _fake_cfg(**over):
    base = dict(
        enabled=True, host="auto", port=8765, admin_token="x" * 40,
        session_timeout_minutes=60, rate_limit=10, log_path="logs/x.log",
        app_executable="python", app_working_directory=".", app_entrypoint="run.py --discord",
        app_control_port=8766, startup_timeout_s=60, shutdown_timeout_s=30,
        default_guild_id="", default_channel_id="", allow_localhost_fallback=False,
        session_file="logs/s.json",
    )
    base.update(over)
    return L.LauncherConfig(**base)


if __name__ == "__main__":
    unittest.main()
