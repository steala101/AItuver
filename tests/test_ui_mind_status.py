import json
import re
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from neuro_voice.ui.webview_app import Api


class MindStatusUiTests(unittest.TestCase):
    @staticmethod
    def _render_storage_capacity(capacity_expression: str) -> str:
        html = (
            Path(__file__).parents[1]
            / "neuro_voice"
            / "ui"
            / "assets"
            / "index.html"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"^function renderStorageCapacity\(capacity\) \{.*?^\}",
            html,
            flags=re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise AssertionError("renderStorageCapacity is not defined")
        script = (
            'function esc(value) { return String(value ?? ""); }\n'
            + match.group(0)
            + "\nconsole.log(renderStorageCapacity("
            + capacity_expression
            + "));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    def test_escape_accepts_numeric_diagnostics(self):
        html = (
            Path(__file__).parents[1]
            / "neuro_voice"
            / "ui"
            / "assets"
            / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('return String(s ?? "").replace(', html)

    def test_mind_overlay_contains_runtime_diagnostics(self):
        html = (
            Path(__file__).parents[1]
            / "neuro_voice"
            / "ui"
            / "assets"
            / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("機能の稼働状況", html)
        self.assertIn("heartbeat_fired_count", html)
        self.assertIn("調査課題なし（正常待機）", html)
        self.assertIn("自律研究・検索履歴", html)
        self.assertIn("researchHistoryLoad", html)
        self.assertIn("get_research_history", html)
        self.assertIn("興味からの次回判定", html)
        self.assertIn("researchSeed.last_reason", html)

    def test_mind_overlay_exposes_content_free_storage_capacity(self):
        html = (
            Path(__file__).parents[1]
            / "neuro_voice"
            / "ui"
            / "assets"
            / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("保存容量", html)
        self.assertIn("warning_threshold_percent", html)
        self.assertIn("measurement_path", html)

    def test_unknown_capacity_renders_null_undefined_and_empty_as_unknown(self):
        backend_payload = {
            "state": "unknown",
            "free_percent": None,
            "warning_threshold_percent": 20.0,
            "measurement_path": "C:\\data",
        }
        expressions = (
            json.dumps(backend_payload),
            '({state:"unknown",free_percent:undefined,'
            'warning_threshold_percent:20,measurement_path:"C:\\\\data"})',
            '({state:"unknown",free_percent:"",'
            'warning_threshold_percent:20,measurement_path:"C:\\\\data"})',
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                rendered = self._render_storage_capacity(expression)
                self.assertIn("保存容量: 不明", rendered)
                self.assertIn("空き: 不明", rendered)
                self.assertNotIn("空き: 0.0%", rendered)

    def test_runtime_diagnostics_survive_mind_status_failure(self):
        class BrokenMind:
            def status(self):
                raise RuntimeError("broken status")

        pipeline = SimpleNamespace(
            runtime_diagnostics=lambda: {
                "proactive_enabled": True,
                "heartbeat": {"task_alive": True},
                "last_evaluation": {"final_action": "do_nothing"},
            }
        )
        backend = SimpleNamespace(
            mind=BrokenMind(), pipeline=pipeline, discord=None,
        )
        result = Api(backend).get_mind_status()
        self.assertFalse(result["enabled"])
        self.assertIn("broken status", result["error"])
        self.assertTrue(result["runtime"]["local"]["heartbeat"]["task_alive"])
