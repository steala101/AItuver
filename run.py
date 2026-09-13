"""Neuro Voice AI 起動スクリプト。

使い方:
    python run.py                  # GUIモード (既定)
    python run.py --cli            # CLI音声会話モード
    python run.py --text           # テキスト入力モード (動作確認用)
    python run.py --list-devices   # オーディオデバイス一覧
"""
import sys


class _SafeWriter:
    """print() 等が壊れた標準出力で落ちないようにする安全ラッパ。

    遠隔ランチャー(pythonw=無コンソール)から子プロセスとして起動されると、
    標準出力ハンドルが無効になり、コード中の print() が
    OSError: [Errno 22] Invalid argument で毎回クラッシュする(応答が無音になる)。
    書き込み/フラッシュで例外が出ても握りつぶし、処理を続行させる。
    """

    def __init__(self, wrapped):
        self._w = wrapped

    def write(self, s):
        try:
            if self._w is not None:
                return self._w.write(s)
        except Exception:
            pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        try:
            if self._w is not None:
                self._w.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        w = object.__getattribute__(self, "_w")
        if w is not None and hasattr(w, name):
            return getattr(w, name)
        return lambda *a, **k: None


def _harden_std() -> None:
    # まず本物のストリームを utf-8 化 (コンソール表示の文字化け対策)
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None:
            try:
                if getattr(stream, "encoding", "").lower() != "utf-8":
                    stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    # 壊れたハンドルでも print() が落ちないよう安全ラッパで包む
    sys.stdout = _SafeWriter(getattr(sys, "stdout", None))
    sys.stderr = _SafeWriter(getattr(sys, "stderr", None))


_harden_std()

# 依存パッケージの確認 (不足時はユーザー確認のうえインストール)
from neuro_voice.bootstrap import check_dependencies

check_dependencies()

from neuro_voice.app import main

if __name__ == "__main__":
    main()
