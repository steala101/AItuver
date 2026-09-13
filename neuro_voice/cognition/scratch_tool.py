"""試験用の書き込み。**作業フォルダの中の、決めた1箇所だけ。**

段階5（書き込み＋確認）を実機で確かめるために要る。ここが無いと
「確認を取ってから実行する」が机上のままになる。

ただし**書き込みは戻せない種類の失敗をする**ので、置ける制約は全部
置いてある。守りたいのは4つ:

* **決めた root の外へ出さない**（`..` も絶対パスも symlink も）
* **既にあるものを壊さない**（排他的作成。上書きしない）
* **正規化した後にもう一度確かめる**（片方だけだと抜ける）
* **root が設定されていなければ、ツールごと止まる**

大きなファイル管理機能は作らない。**1件のテキストを作るだけ。**
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neuro_voice.cognition.tool_report import content_digest

#: 1件の上限。試験用なので小さくてよい。
MAX_BYTES = 8 * 1024
#: 相対パスの深さ。**深い階層を掘らせない。**
MAX_DEPTH = 3
#: 許すのはテキストだけ。
ALLOWED_SUFFIXES = frozenset({".txt", ".md", ".log"})
#: ファイル名に許す文字。**記号で遊ばせない。**
_ALLOWED_NAME = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_. ")


class ScratchError(Exception):
    """安全境界に触れた。**理由を必ず持つ。**"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


@dataclass(frozen=True, slots=True)
class ScratchFile:
    created_path: str
    relative_path: str
    content_digest: str
    byte_size: int
    created_by_execution_id: str = ""
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        """**中身は入れない。** 指紋と場所だけ。"""
        return {
            "created_path": self.created_path,
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "byte_size": self.byte_size,
            "created_by_execution_id": self.created_by_execution_id,
        }


def normalise_relative(value: str) -> str:
    """相対パスを1つの形へ。**ここで弾けるものは全部弾く。**

    正規化した「後」に確かめ直すのが要点。`a/../../b` は正規化前だと
    深さ2に見えるが、実際は root の外。
    """
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise ScratchError("empty_path")
    if raw.startswith("/") or raw.startswith("~"):
        raise ScratchError("absolute_path_not_allowed")
    if len(raw) > 2 and raw[1] == ":":
        # Windows のドライブ指定。
        raise ScratchError("absolute_path_not_allowed")
    if "\x00" in raw:
        raise ScratchError("null_byte")

    parts = [item for item in raw.split("/") if item not in {"", "."}]
    if not parts:
        raise ScratchError("empty_path")
    if any(item == ".." for item in parts):
        raise ScratchError("path_traversal_not_allowed")
    if len(parts) > MAX_DEPTH:
        raise ScratchError("too_deep")
    for item in parts:
        if not set(item) <= _ALLOWED_NAME:
            raise ScratchError("invalid_character")
        if item.startswith("."):
            raise ScratchError("hidden_file_not_allowed")

    name = parts[-1]
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ScratchError("suffix_not_allowed")
    normalised = "/".join(parts)
    # **正規化の後にもう一度。** 上の判定を通ってからでも、
    # 実体が別の場所を指していることはある（下の resolve で見る）。
    if ".." in normalised.split("/"):
        raise ScratchError("path_traversal_not_allowed")
    return normalised


class ScratchWriter:
    """`test_scratch_root` の中に、テキストを1件作るだけ。"""

    def __init__(self, root: str | os.PathLike[str] | None) -> None:
        self.root: Path | None = Path(root).expanduser() if root else None

    @property
    def enabled(self) -> bool:
        """**root が設定されていなければ、ツールごと止まる。**"""
        return self.root is not None

    def _resolved_root(self) -> Path:
        if self.root is None:
            raise ScratchError("scratch_root_not_configured")
        root = self.root.resolve()
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ScratchError("scratch_root_not_a_directory")
        return root

    def resolve(self, relative_path: str) -> tuple[Path, Path]:
        """書き先を決める。**symlink を辿った後の実体で確かめる。**

        文字列の判定（`normalise_relative`）だけでは足りない。
        `notes/link/a.txt` の `link` が外を指す symlink なら、文字列は
        きれいなまま実体が root の外になる。**存在する所まで実体化して
        から確かめる。**
        """
        root = self._resolved_root()
        target = root / normalise_relative(relative_path)
        if target.is_symlink():
            raise ScratchError("symlink_not_allowed")

        probe = target.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        anchor = probe.resolve()
        if anchor != root and root not in anchor.parents:
            raise ScratchError("symlink_escapes_root")

        resolved = anchor / target.relative_to(probe)
        if resolved != root and root not in resolved.parents:
            raise ScratchError("outside_scratch_root")
        return root, resolved

    def create_text(self, *, relative_path: str, content: str,
                    execution_id: str = "") -> ScratchFile:
        """1件だけ作る。**上書きしない。**"""
        text = str(content or "")
        data = text.encode("utf-8")
        if not data:
            raise ScratchError("empty_content")
        if len(data) > MAX_BYTES:
            raise ScratchError("content_too_large")

        _root, resolved = self.resolve(relative_path)
        if resolved.exists():
            # **既にあるものを壊さない。** 上書きは「作成」ではない。
            raise ScratchError("file_already_exists")
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # 排他的作成。`exists()` の後に誰かが作っても、ここで失敗する。
        try:
            handle = os.open(resolved, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise ScratchError("file_already_exists") from error
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
        except Exception as error:                      # noqa: BLE001
            raise ScratchError(f"write_failed:{type(error).__name__}") from error

        return ScratchFile(
            created_path=str(resolved),
            relative_path=normalise_relative(relative_path),
            content_digest=content_digest(data), byte_size=len(data),
            created_by_execution_id=str(execution_id))

    def exists(self, relative_path: str) -> bool:
        try:
            _root, resolved = self.resolve(relative_path)
        except ScratchError:
            return False
        return resolved.is_file()

    def rollback(self, record: ScratchFile) -> str:
        """作ったものだけを消す。**迷ったら消さない。**

        「掃除しておいた」で消える方が、残っているより困る。
        """
        if not record.created_by_execution_id:
            return "unknown_creator"
        try:
            _root, resolved = self.resolve(record.relative_path)
        except ScratchError as error:
            return error.reason
        if str(resolved) != record.created_path:
            return "path_changed"
        if not resolved.is_file():
            return "already_gone"
        if content_digest(resolved.read_bytes()) != record.content_digest:
            # **作った後に中身が変わっている。** 別の誰かが使っている。
            return "content_changed"
        resolved.unlink()
        return "removed"


def scratch_handler(writer: ScratchWriter):
    """`test_scratch_create_text.create` の実物。

    返す `dict` が `ToolResult.structured_output` になる。
    `created_path` と `content_digest` は検証の根拠側へ振り分けられる。
    """

    def _create(*, relative_path: str, content: str, **_extra) -> dict[str, Any]:
        record = writer.create_text(relative_path=relative_path,
                                    content=content)
        return {
            "created_path": record.created_path,
            "relative_path": record.relative_path,
            "content_digest": record.content_digest,
            "byte_size": record.byte_size,
            "exists": True,
        }

    return _create


__all__ = [
    "ALLOWED_SUFFIXES", "MAX_BYTES", "MAX_DEPTH", "ScratchError",
    "ScratchFile", "ScratchWriter", "normalise_relative", "scratch_handler",
]
