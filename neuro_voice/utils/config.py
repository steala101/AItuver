"""config.yaml の読み込みと環境変数展開。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_RUNTIME_OVERRIDES = {
    "AITUBER_LLM_MODEL": "llm.backends.ollama.model",
    "AITUBER_VISION_ENABLED": "vision.enabled",
    "AITUBER_VIDEO_ENABLED": "video.enabled",
    "AITUBER_VIDEO_SOURCE": "video.source",
    "AITUBER_AUDIO_INPUT_BACKEND": "audio_input.backend",
    "AITUBER_AUDIO_OUTPUT_BACKEND": "audio_output.backend",
    "AITUBER_VOICEVOX_URL": "audio_output.voicevox.base_url",
}


def _expand_env(value: Any) -> Any:
    """文字列中の ${VAR} を環境変数で置換する。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _set_dotted(data: dict[str, Any], key: str, value: Any) -> None:
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _apply_runtime_overrides(data: dict[str, Any]) -> dict[str, Any]:
    for env, key in _RUNTIME_OVERRIDES.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        value: Any = raw
        if key.endswith(".enabled"):
            value = raw.strip().lower() in {"1", "true", "yes", "on"}
        elif key == "video.source":
            try:
                value = int(raw)
            except ValueError:
                value = raw
        _set_dotted(data, key, value)
    return data


class Config:
    """ドット区切りキーでアクセスできる設定ラッパー。"""

    def __init__(self, data: dict[str, Any], path: str | Path | None = None):
        self._data = data
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path | None:
        """読み込み元の config.yaml パス (ファイル保存用)。"""
        return self._path

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        load_dotenv()
        config_path = Path(path).resolve()
        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(_apply_runtime_overrides(_expand_env(raw)), path=config_path)

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        """実行時に設定値を書き換える (ファイルへの保存は別途)。"""
        node = self._data
        parts = key.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    def section(self, key: str) -> dict[str, Any]:
        value = self.get(key, {})
        return value if isinstance(value, dict) else {}

    def persist(self, dotted_key: str, value: Any) -> bool:
        """実行時に set した値を、読み込み元の config.yaml へ書き戻す。

        コメント・インデント構造を保ったままスカラー行だけを書き換える。
        path が未設定なら何もしない。
        """
        self.set(dotted_key, value)
        return persist_yaml_value(self._path, dotted_key, value)


def persist_yaml_value(config_path: str | Path | None, dotted_key: str, value: Any) -> bool:
    """config.yaml の指定キーの値を、コメント・構造を保ったまま書き換える。

    対象はスカラー値の行のみ (例: "stt.device")。成功で True。
    GUI (webview) とヘッドレス (Discord遠隔) の双方から使う共有ヘルパー。
    """
    if config_path is None:
        return False
    path = Path(config_path)
    if not path.exists():
        return False
    try:
        keys = dotted_key.split(".")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        stack: list[tuple[int, str]] = []  # (インデント幅, キー名)
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)([A-Za-z0-9_]+):(.*)$", line)
            if m is None:
                continue
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(2)))
            if [k for _, k in stack] == keys:
                rest = m.group(3)
                cm = re.search(r"(\s*#.*?)\s*$", rest)
                comment = cm.group(1) if cm else ""
                lines[i] = f"{m.group(1)}{m.group(2)}: {value}{comment}\n"
                path.write_text("".join(lines), encoding="utf-8")
                return True
        return False
    except Exception:
        return False
