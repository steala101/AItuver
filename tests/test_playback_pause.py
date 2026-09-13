"""Discord再生 pause/resume の世代管理 (pause_token) 不変条件のテスト。

bot.py 本体はDiscord依存で単体テストが難しいため、ここでは pause_token と
「新応答開始時リセット」のロジックを最小のスタンドインで再現し、指示書の
再現テスト1・2・4・5に対応する不変条件を固定する。実装 (bot.py) は同じ規則に
従う。
"""
import unittest


class Encoder:
    """Discordサイドカーencoderの最小モデル (paused/バッファ)。"""

    def __init__(self):
        self.paused = False
        self.buffer = []          # (generation, pcm)
        self.played = []          # 実際に再生されたPCM

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        # 溜まっていた分を吐き出す (paused中は再生されない挙動の再現)
        for gen, pcm in self.buffer:
            self.played.append((gen, pcm))
        self.buffer.clear()

    def stop(self):           # バッファ破棄 + paused解除
        self.paused = False
        self.buffer.clear()

    def play(self, gen, pcm):
        if self.paused:
            self.buffer.append((gen, pcm))
        else:
            self.played.append((gen, pcm))


class Bridge:
    """bot.py の pause/resume/reset ロジックの本質だけを写したモデル。"""

    def __init__(self):
        self.enc = Encoder()
        self.paused = False
        self.pause_token = 0
        self.active_generation = 0

    def on_speech_start(self, assistant_active):
        if self.paused or not assistant_active:
            return
        self.paused = True
        self.pause_token += 1
        self.enc.pause()

    def resume_if_pending(self, token=None):
        if not self.paused:
            return
        if token is not None and token != self.pause_token:
            return  # 古いresumeは無視 (指示書§7)
        self.paused = False
        self.enc.resume()

    def reset_for_new_response(self):
        if not self.paused:
            return
        self.paused = False
        self.pause_token += 1
        self.enc.stop()  # 古い音声を破棄 (遅延再生を根絶)

    def begin_response(self):
        self.active_generation += 1
        self.reset_for_new_response()

    def speak(self, gen, pcm):
        if gen != self.active_generation:
            return  # 世代違いは破棄 (指示書§9)
        self.enc.play(gen, pcm)


class PauseTokenTests(unittest.TestCase):
    def test_double_pause_is_idempotent(self):
        b = Bridge()
        b.on_speech_start(assistant_active=True)
        t1 = b.pause_token
        b.on_speech_start(assistant_active=True)  # PAUSED中の再pause
        self.assertEqual(b.pause_token, t1)        # tokenは変わらない
        self.assertTrue(b.paused)

    def test_stale_resume_ignored(self):
        b = Bridge()
        b.on_speech_start(assistant_active=True)
        old = b.pause_token
        b.resume_if_pending(token=old)             # 正常resume
        b.on_speech_start(assistant_active=True)    # 新pause (token=2)
        b.resume_if_pending(token=old)             # 古いtoken → 無視
        self.assertTrue(b.paused)                  # 新pauseは維持
        self.assertNotEqual(b.pause_token, old)

    def test_new_response_resets_pause_and_plays_fresh(self):
        b = Bridge()
        # 応答Aを再生開始 → pauseされ、AのTTSがバッファに溜まる
        b.begin_response()                          # gen=1
        b.on_speech_start(assistant_active=True)    # pause
        b.speak(1, "A-tail")                        # paused中 → バッファへ
        self.assertEqual(b.enc.played, [])
        # AI発話が自然終了し、pause未解決のまま新応答Bが始まる
        b.begin_response()                          # gen=2 + reset (古いA破棄)
        self.assertFalse(b.paused)
        b.speak(2, "B-1")
        b.speak(1, "A-late")                        # 古い世代 → 破棄
        self.assertEqual(b.enc.played, [(2, "B-1")])  # Bだけが再生・Aは遅延再生しない

    def test_hard_interrupt_discards_old_generation(self):
        b = Bridge()
        b.begin_response()                          # gen=1
        b.speak(1, "old")
        b.begin_response()                          # gen=2 (割り込み後の新回答相当)
        b.speak(1, "old-late")                      # 破棄
        b.speak(2, "new")
        self.assertEqual([g for g, _ in b.enc.played], [1, 2])
        self.assertNotIn("old-late", [p for _, p in b.enc.played])


if __name__ == "__main__":
    unittest.main()
