"""remote_launcher をタスクトレイ (通知領域) に常駐させ、クリックで状態/ログを表示する。

pystray (トレイ) + tkinter (ウィンドウ) を使う。どちらも標準では無い場合があるため、
インポートに失敗したら tray_available() が False を返し、呼び出し側はヘッドレスへ
フォールバックする。

- トレイアイコンをクリック / 「状態とログを表示」→ ウィンドウを開く
  (上: ランチャー/AI本体/Discordの現在状態、下: ログの末尾。2秒ごとに自動更新)
- 「ログフォルダを開く」→ エクスプローラでログの場所を開く
- 「終了」→ APIサーバーとランチャーを停止
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger("remote_launcher.tray")

# AI本体ログ (neuro_voice.log) から、会話・割り込み・エラー等の「見たい行」だけ抽出する。
# primp/faster_whisper 等の大量のノイズ行は除外して会話を追いやすくする。
_AI_KEEP = re.compile(
    r"🗣|TTS合成|discord_reply|Discord話者判定|Discord発話|Conversation decision|"
    r"barge-in|割り込み|相槌|相づち|FD |再生をリセット|古いresume|エコー|回り込み|"
    r"聞き手|繋ぎ発話|話者を|ペルソナ|ERROR|WARNING|Traceback|Discord応答を見送り|"
    r"⏱|LLM初回|合計|ロード完了|GPUでのSTT初期化に失敗|フォールバック"
)
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def tray_available() -> bool:
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


class LauncherTray:
    """トレイ常駐 + 状態/ログ表示ウィンドウ。"""

    def __init__(self, *, controller, cfg, log_path: str, host: str, port: int,
                 on_quit, ai_log_path: str | None = None):
        self._controller = controller
        self._cfg = cfg
        self._log_path = Path(log_path)
        # AI本体 (run.py --discord) の会話ログ。既定は logs/neuro_voice.log
        self._ai_log_path = Path(ai_log_path) if ai_log_path \
            else (self._log_path.parent / "neuro_voice.log")
        self._host = host
        self._port = port
        self._on_quit = on_quit
        self._root = None       # tkinter root (メインスレッド)
        self._window = None     # ログウィンドウ (Toplevel)
        self._text = None
        self._status_var = None
        self._icon = None
        self._stopping = False

    # ---------- 起動 ----------

    def run(self) -> None:
        """メインスレッドで実行 (tkinter mainloop)。トレイは別スレッド。"""
        import tkinter as tk

        self._root = tk.Tk()
        self._root.withdraw()               # ルートは常に非表示 (トレイ常駐)
        self._root.title("Neuro Remote Launcher")
        self._status_var = tk.StringVar(value="起動中…")

        self._icon = self._make_icon()
        threading.Thread(target=self._icon.run, name="tray-icon", daemon=True).start()
        logger.info("トレイ常駐を開始しました (通知領域のアイコンをクリックで状態表示)")
        try:
            self._root.mainloop()
        finally:
            self._shutdown()

    def _make_icon(self):
        import pystray
        from PIL import Image

        icon_file = Path(__file__).parent / "static" / "icon-192.png"
        try:
            image = Image.open(icon_file)
        except Exception:
            image = Image.new("RGBA", (64, 64), (34, 211, 238, 255))

        menu = pystray.Menu(
            pystray.MenuItem("状態とログを表示", self._on_show, default=True),
            pystray.MenuItem("ログフォルダを開く", self._on_open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", self._on_exit),
        )
        return pystray.Icon("neuro_remote_launcher", image,
                            "Neuro Remote Launcher", menu)

    # ---------- メニュー操作 (トレイスレッドから呼ばれる → tkinterへ委譲) ----------

    def _on_show(self, *_a):
        if self._root is not None:
            self._root.after(0, self._show_window)

    def _on_open_logs(self, *_a):
        with __import__("contextlib").suppress(Exception):
            folder = self._log_path.parent.resolve()
            os.startfile(str(folder))  # Windows

    def _on_exit(self, *_a):
        if self._root is not None:
            self._root.after(0, self._quit)

    # ---------- ウィンドウ ----------

    def _show_window(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        if self._window is not None and tk.Toplevel.winfo_exists(self._window):
            self._window.deiconify()
            self._window.lift()
            self._refresh()
            return

        win = tk.Toplevel(self._root)
        self._window = win
        win.title(f"Neuro Remote Launcher  —  http://{self._host}:{self._port}")
        win.geometry("960x640")
        win.minsize(560, 360)
        win.configure(bg="#0a0d16")

        status = tk.Label(win, textvariable=self._status_var, justify="left",
                          anchor="w", bg="#0a0d16", fg="#e8ecf5",
                          font=("Consolas", 10), padx=12, pady=10)
        status.pack(fill="x")

        self._text = scrolledtext.ScrolledText(
            win, wrap="word", bg="#121627", fg="#9aa6c0",
            insertbackground="#e8ecf5", font=("Consolas", 10), borderwidth=0)
        self._text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        # 読み取り専用だがマウス選択とコピー(Ctrl+C)は可能にする
        self._text.bind("<Key>", self._readonly_key)

        bar = tk.Frame(win, bg="#0a0d16")
        bar.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(bar, text="更新", command=self._refresh).pack(side="left")
        tk.Button(bar, text="ログフォルダ", command=lambda: self._on_open_logs()).pack(side="left", padx=6)
        tk.Button(bar, text="閉じる", command=win.withdraw).pack(side="right")

        # 閉じる(×)はトレイへ格納 (アプリは終了しない)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self._refresh()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._root is None or self._stopping:
            return
        # ウィンドウが表示されている間だけ2秒ごとに更新
        try:
            import tkinter as tk
            if self._window is not None and tk.Toplevel.winfo_exists(self._window) \
                    and self._window.state() != "withdrawn":
                self._refresh()
        except Exception:
            pass
        self._root.after(1500, self._schedule_refresh)

    @staticmethod
    def _readonly_key(event):
        # コピー(Ctrl+C / Ctrl+A)と移動キーは許可、それ以外の編集は無効化
        if (event.state & 0x4) and event.keysym.lower() in ("c", "a"):  # Ctrl+C / Ctrl+A
            return
        if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                             "Prior", "Next", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        return "break"

    def _refresh(self) -> None:
        # 状態
        try:
            snap = self._controller.state.snapshot()
            self._status_var.set(
                f"待受: http://{self._host}:{self._port}   (Tailscale内のみ)\n"
                f"AI本体: {snap.get('app_state')}   Ready: {snap.get('app_ready')}\n"
                f"Discord: {snap.get('discord_state')}   "
                f"{(snap.get('guild_name') or '')}/{(snap.get('channel_name') or '')}\n"
                f"{self._runtime_text()}\n"
                f"実行中: {snap.get('current_operation') or 'なし'}   "
                f"最後のエラー: {snap.get('last_error') or 'なし'}"
            )
        except Exception:
            self._status_var.set("状態を取得できません")
        # ログ末尾 (会話+ランチャー)
        if self._text is not None:
            # 文字選択中は更新しない (コピーのために選択が消えるのを防ぐ)
            try:
                if self._text.tag_ranges("sel"):
                    return
            except Exception:
                pass
            tail = self._read_log_tail(120)
            if tail == getattr(self, "_last_tail", None):
                return  # 内容が変わっていなければ書き換えない
            self._last_tail = tail
            # 末尾を見ていたときだけ自動スクロールする (上を読んでいる最中は動かさない)
            at_bottom = True
            try:
                at_bottom = self._text.yview()[1] >= 0.999
            except Exception:
                pass
            self._text.delete("1.0", "end")
            self._text.insert("1.0", tail)
            if at_bottom:
                self._text.see("end")

    def _runtime_text(self) -> str:
        """STT/TTS/LLM が GPU/CPU どちらで動くかを1行にまとめる (4秒キャッシュ)。"""
        now = time.monotonic()
        if (now - getattr(self, "_rt_at", 0.0) < 4.0
                and getattr(self, "_rt_cache", None) is not None):
            return self._rt_cache

        def gpu(on: bool, dev) -> str:
            return "GPU" if on else ("CPU" if dev else "?")

        line = "実行環境: 取得中…"
        try:
            ri = self._controller.runtime_info()
            if ri.get("ok"):
                stt = ri.get("stt") or {}
                tts = ri.get("tts") or {}
                llm = ri.get("llm") or {}
                stt_txt = gpu(stt.get("on_gpu"), stt.get("device"))
                if stt.get("fell_back"):
                    stt_txt = "CPU(GPU失敗)"
                eng = tts.get("engine", "")
                if eng == "style_bert_vits2":
                    tts_txt = "SBV2/" + gpu(tts.get("on_gpu"), tts.get("device"))
                elif eng == "voicevox":
                    tts_txt = "VOICEVOX"
                else:
                    tts_txt = eng or "-"
                llm_txt = (llm.get("model") or "-")
                # OllamaのGPU/CPU配分。100%未満=CPU退避(応答直前のCPU跳ね・遅延の主因)
                pct = llm.get("gpu_percent")
                if pct is not None:
                    llm_txt += f" (GPU{pct}%{'' if llm.get('fully_gpu') else '⚠CPU退避'})"
                tag = "設定" if ri.get("offline") else "実稼働"
                line = f"実行環境[{tag}]: STT={stt_txt}  TTS={tts_txt}  LLM={llm_txt}"
                gpu = ri.get("gpu") or {}
                if gpu.get("vram_total_mb"):
                    warn = "⚠VRAM満杯(ページング)" if gpu.get("vram_tight") else ""
                    line += (f"\nGPU: VRAM {gpu.get('vram_used_mb')}/{gpu.get('vram_total_mb')}MB"
                             f"({gpu.get('vram_percent')}%){warn}  使用率{gpu.get('gpu_util')}%")
            else:
                line = "実行環境: " + (ri.get("message") or "取得できません")
        except Exception:
            line = "実行環境: 取得できません"
        self._rt_cache = line
        self._rt_at = now
        return line

    @staticmethod
    def _tail_lines(path: Path, n: int) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.readlines()[-n:]
        except Exception:
            return []

    def _read_log_tail(self, lines: int) -> str:
        # ランチャー(監査含む)ログ + AI本体の会話ログ(抽出) を時刻順にマージする。
        launcher = [("L", ln) for ln in self._tail_lines(self._log_path, 300)]
        ai = [("P", ln) for ln in self._tail_lines(self._ai_log_path, 600)
              if _AI_KEEP.search(ln)]
        merged = launcher + ai
        if not merged:
            return "(ログはまだありません。AI本体を起動すると会話が表示されます)"

        def key(item):
            m = _TS.match(item[1])
            return m.group(1) if m else ""

        merged.sort(key=key)
        out = []
        for src, ln in merged[-lines:]:
            tag = "ポッポ" if src == "P" else "launcher"
            out.append(f"[{tag}] {ln.rstrip()}")
        return "\n".join(out)

    # ---------- 終了 ----------

    def _quit(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        with __import__("contextlib").suppress(Exception):
            self._on_quit()
        with __import__("contextlib").suppress(Exception):
            if self._icon is not None:
                self._icon.stop()
        with __import__("contextlib").suppress(Exception):
            if self._root is not None:
                self._root.quit()

    def _shutdown(self) -> None:
        with __import__("contextlib").suppress(Exception):
            if self._icon is not None:
                self._icon.stop()
