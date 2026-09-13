"""起動時の依存パッケージ確認。

不足パッケージがあれば一覧を表示し、ユーザーの確認を取ってから
pip インストールする。PyTorch はバージョン違い (CPU版/CUDA不一致) で
環境を壊しやすいため自動インストールせず、手動コマンドを案内する。

このモジュールは標準ライブラリのみに依存すること。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys

# (import名, pipパッケージ指定, 用途)
REQUIRED: list[tuple[str, str, str]] = [
    ("numpy", "numpy", "数値計算"),
    ("sounddevice", "sounddevice", "マイク/スピーカー入出力"),
    ("soundcard", "soundcard", "Discord通話のループバック取り込み(聞き取り)"),
    ("soundfile", "soundfile", "音声データ処理"),
    ("yaml", "PyYAML", "設定ファイル読込"),
    ("dotenv", "python-dotenv", "APIキー読込"),
    ("openai", "openai>=1.40", "LLM API (Cerebras/Ollama等)"),
    ("httpx", "httpx>=0.27", "Ollamaマルチモーダル通信"),
    ("silero_vad", "silero-vad>=5.1", "発話検出 (VAD)"),
    ("faster_whisper", "faster-whisper>=1.1", "音声認識 (STT)"),
    ("requests", "requests", "VOICEVOX連携"),
    ("webview", "pywebview>=5.0", "GUI"),
    ("mss", "mss", "画面キャプチャ (Vision)"),
    ("PIL", "Pillow", "画像処理 (Vision)"),
    ("ddgs", "ddgs", "Web検索 (DeepSearch)"),
    ("sentence_transformers", "sentence-transformers>=3.0", "長期記憶の意味検索 (Mind)"),
    ("onnxruntime", "onnxruntime", "話者識別 (声で相手を覚える)"),
    # 2026-03のDAVE(E2EE)強制以降、DAVE非対応のdiscord.py 2.6以前は音声サーバーに
    # 接続を維持できず即切断ループになる。2.7系はDAVE対応で接続・送信(TTS)が安定する。
    # ※ただし受信拡張(voice-recv)は現状DAVEの復号に未対応のため、相手の声の
    #   聞き取りはできない (システム音声のループバック等での代替が必要)。
    ("discord", "discord.py[voice]==2.7.1", "Discord連携 (VC参加・音声送信)"),
    ("discord.ext.voice_recv", "discord-ext-voice-recv", "Discord音声の受信 (聞き取り)"),
    ("yt_dlp", "yt-dlp", "VCで音楽再生 (YouTube検索。別途FFmpegが必要)"),
]

_TORCH_CMD = "pip install torch --index-url https://download.pytorch.org/whl/cu128"

#: Import names whose presence is cheaper to establish from installed
#: distribution metadata than by locating the module.  ``find_spec`` on a
#: submodule imports its parents, and importing ``discord`` at startup is
#: several seconds the user spends staring at nothing.
_DISTRIBUTION_FOR: dict[str, str] = {
    "discord": "discord.py",
    "discord.ext.voice_recv": "discord-ext-voice-recv",
}


def _distribution_installed(name: str) -> bool:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:      # pragma: no cover - Python < 3.8 only
        return True
    try:
        version(name)
    except PackageNotFoundError:
        return False
    except Exception:
        # An unreadable environment must not block startup over a warning.
        return True
    return True


def discord_py_version() -> str:
    """The installed discord.py version, read without importing it."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:      # pragma: no cover
        return ""
    try:
        return str(version("discord.py"))
    except PackageNotFoundError:
        return ""
    except Exception:
        return ""


def check_dependencies() -> None:
    """不足パッケージを確認し、必要ならユーザー確認のうえインストールする。"""
    if importlib.util.find_spec("torch") is None:
        print("⚠ PyTorch が見つかりません。")
        print("  GPU (RTX 50シリーズ = CUDA 12.8) 対応版を手動でインストールしてください:")
        print(f"    {_TORCH_CMD}")
        print("  ※ 誤ったビルドで環境を壊さないよう、自動インストールは行いません。")
        sys.exit(1)

    def _installed(import_name: str) -> bool:
        # A submodule check makes find_spec import every parent package, and
        # ``discord`` alone costs seconds of startup before the window appears.
        # Distribution metadata answers the same question without importing.
        distribution = _DISTRIBUTION_FOR.get(import_name)
        if distribution is not None:
            return _distribution_installed(distribution)
        try:
            return importlib.util.find_spec(import_name) is not None
        except ModuleNotFoundError:
            # 親パッケージごと未導入の場合 ("discord.ext.voice_recv" 等)
            return False

    missing = [item for item in REQUIRED if not _installed(item[0])]
    if not missing:
        _check_discord_compat()  # 全部導入済みでもバージョン互換は確認する
        return

    pip_specs = [spec for _, spec, _ in missing]
    print("以下のPythonパッケージが不足しています:\n")
    for _, spec, desc in missing:
        print(f"  - {spec:<24} … {desc}")
    print()

    manual_cmd = "pip install " + " ".join(f'"{s}"' for s in pip_specs)
    if not sys.stdin.isatty():
        print("対話環境ではないため自動インストールは行いません。以下を実行してください:")
        print(f"  {manual_cmd}")
        sys.exit(1)

    answer = input("これらを pip でインストールしますか? (既存環境に影響する可能性があります) [y/N]: ")
    if answer.strip().lower() not in ("y", "yes"):
        print("\nインストールを中止しました。手動で導入する場合:")
        print(f"  {manual_cmd}")
        sys.exit(1)

    cmd = [sys.executable, "-m", "pip", "install", *pip_specs]
    print("\n実行中:", " ".join(cmd), "\n")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("\n⚠ インストールに失敗しました。上記のエラーを確認してください。")
        sys.exit(1)
    print("\n✅ インストール完了。起動を続行します。\n")

    if importlib.util.find_spec("qwen_tts") is None:
        print("(参考) qwen-tts は未導入です。tts.backend: qwen3 を使う場合のみ必要です。\n")

    _check_discord_compat()


def _check_discord_compat() -> None:
    """discord.py のバージョン互換チェック。

    2026-03のDAVE(E2EE)強制以降、接続維持にはDAVE対応の2.7系が必須。
    2.6以前は即切断ループになる。
    (受信=相手の声の聞き取りは、voice-recvがDAVE未対応のため別途ループバック等で代替)
    """
    # Read the version from metadata rather than importing discord, which
    # costs seconds before the window is even drawn.
    ver = discord_py_version()
    if not ver:
        return
    if ver.startswith("2.7") or ver.startswith("2.8"):
        return
    cmd = 'pip install "discord.py[voice]==2.7.1"'
    print(f"⚠ discord.py {ver} はDAVE(E2EE)未対応で、現在のDiscord音声サーバーに")
    print("  接続を維持できません (VCから即切断される出入りループ)。")
    if not sys.stdin.isatty():
        print(f"  以下を実行してください: {cmd}")
        return
    answer = input("  対応バージョン 2.7.1 に入れ替えますか? [y/N]: ")
    if answer.strip().lower() in ("y", "yes"):
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "discord.py[voice]==2.7.1"]
        )
        print("✅ 入れ替え完了。\n" if result.returncode == 0
              else "⚠ 入れ替えに失敗しました。手動で実行してください: " + cmd + "\n")
