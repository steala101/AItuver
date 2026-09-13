"""疑似フルデュプレックス: 割り込み語/相づち/エコーガードのテスト (フェーズ2+4+5)。"""
import unittest

import numpy as np

from neuro_voice.memory.interaction import InteractionClassifier, SpeechIntent
from neuro_voice.realtime.full_duplex import (
    AcousticEchoGuard, ConversationControlLayer, InterruptKeywordDetector,
    endpoint_hint,
)


class InterruptKeywordTests(unittest.TestCase):
    def setUp(self):
        self.d = InterruptKeywordDetector()

    def test_immediate_words_fire_on_interim(self):
        for text in ("待って", "ちょっと待って", "あっ、待って！", "ストップ",
                     "止めて", "やめて", "一旦止めて", "マッテ"):
            self.assertTrue(self.d.is_hard_interrupt(text), text)

    def test_backchannels_never_fire(self):
        for text in ("うん", "うんうん", "はい", "なるほど", "へえ", "そうなんだ",
                     "ふーん", "ええ", "おお", "笑", "ははは"):
            self.assertFalse(self.d.is_hard_interrupt(text), text)
            self.assertFalse(self.d.is_hard_interrupt(text, final=True), text)

    def test_final_only_words(self):
        # interimでは発火しない (誤認リスク) が、確定STTでは割り込み扱い
        self.assertFalse(self.d.is_hard_interrupt("違う"))
        self.assertTrue(self.d.is_hard_interrupt("違う", final=True))
        self.assertTrue(self.d.is_hard_interrupt("違う違う", final=True))
        self.assertTrue(self.d.is_hard_interrupt("いやいや", final=True))
        self.assertTrue(self.d.is_hard_interrupt("そうじゃなくて", final=True))

    def test_leading_filler_not_confused(self):
        # 「いや、それでさ…」のような接頭の相づちは割り込みにしない
        self.assertFalse(self.d.is_hard_interrupt("いや、それでさ昨日の話なんだけど", final=True))
        # 長い文は既存の実質発話分類に任せる
        self.assertFalse(self.d.is_hard_interrupt(
            "待ってといえば昨日駅で待っていたら面白いことがあってね", final=True))

    def test_normalization(self):
        self.assertTrue(self.d.is_hard_interrupt("ｽﾄｯﾌﾟ"))          # 半角カナ
        self.assertTrue(self.d.is_hard_interrupt("待っ て"))          # 空白混入
        self.assertTrue(self.d.is_hard_interrupt("止めて。", final=True))

    def test_extra_words(self):
        # 追加語はSTTの表記 (漢字含む) に合わせて登録する。カタカナ・全半角は吸収される
        d = InterruptKeywordDetector(["ぽっぽ止まれ"])
        self.assertTrue(d.is_hard_interrupt("ポッポ止まれ！"))
        self.assertFalse(self.d.is_hard_interrupt("ポッポ止まれ"))  # 既定語彙には無い


class BackchannelTests(unittest.TestCase):
    """フェーズ4: 複合相づち・笑い声の判定。"""

    def setUp(self):
        self.c = InteractionClassifier()

    def test_composite_acks_continue_playback(self):
        for text in ("うんうんそうそう", "はいはい", "うんそうだね",
                     "なるほどね、うんうん", "ああ、なるほどなるほどね"):
            r = self.c.classify(text)
            self.assertEqual(r.intent, SpeechIntent.ACKNOWLEDGEMENT, (text, r.reason))

    def test_ack_plus_content_is_interruption(self):
        # 「うん、でもさ」= 発言権を取りたい発話。相づちに飲み込まない
        for text in ("うん、でもさ", "そうだね、ところで", "なるほど、じゃあ質問"):
            r = self.c.classify(text)
            self.assertEqual(r.intent, SpeechIntent.INTERRUPTION, (text, r.reason))

    def test_laughter_is_false_positive(self):
        for text in ("ははは", "あはは", "ふふふ", "わははは", "笑", "www"):
            r = self.c.classify(text)
            self.assertEqual(r.intent, SpeechIntent.FALSE_POSITIVE, (text, r.reason))
            self.assertEqual(r.reason, "laughter")


class EchoGuardTests(unittest.TestCase):
    """フェーズ5: 包絡相関によるスピーカー回り込み判定。"""

    @staticmethod
    def _patterned(pattern, sr=16000, seg_s=0.2, seed=0):
        # パターン (音量列) で振幅変調したノイズ = 抑揚のある擬似音声
        rng = np.random.default_rng(seed)
        segs = [rng.standard_normal(int(sr * seg_s)).astype(np.float32) * amp
                for amp in pattern]
        return np.concatenate(segs)

    def test_echo_scores_high_and_unrelated_low(self):
        guard = AcousticEchoGuard(window_s=6.0)
        pattern = [0.9, 0.1, 0.7, 0.2, 0.8, 0.05, 0.6, 0.3, 0.9, 0.1]
        tts = self._patterned(pattern, seed=1)
        guard.add_reference(tts, 16000)

        # 回り込み: 同じ抑揚パターンが減衰+ノイズ付きで戻ってくる
        echo = self._patterned(pattern, seed=2) * 0.3
        echo_score = guard.score(echo, 16000)

        # 無関係な発話: 異なる抑揚パターン
        other = self._patterned([0.3, 0.8, 0.2, 0.2, 0.1, 0.9, 0.4, 0.7, 0.1, 0.5], seed=3)
        other_score = guard.score(other, 16000)

        self.assertGreater(echo_score, 0.8, f"echo={echo_score}")
        self.assertLess(other_score, 0.6, f"other={other_score}")
        self.assertGreater(echo_score, other_score + 0.2)

    def test_short_audio_returns_zero(self):
        guard = AcousticEchoGuard()
        guard.add_reference(np.ones(16000, dtype=np.float32), 16000)
        self.assertEqual(guard.score(np.ones(1600, dtype=np.float32), 16000), 0.0)

    def test_empty_reference_returns_zero(self):
        guard = AcousticEchoGuard()
        self.assertEqual(guard.score(np.ones(16000, dtype=np.float32), 16000), 0.0)


class EndpointHintTests(unittest.TestCase):
    """フェーズ6: 途中結果の語尾による発話終了ヒント。"""

    def test_continuation_extends(self):
        for text in ("昨日行ったんだけど", "それで", "つまり", "ゲームの話なんですが",
                     "えっと", "うーん", "あの店はなんか"):
            self.assertEqual(endpoint_hint(text), "continue", text)

    def test_final_shortens(self):
        for text in ("今日は暑いですね", "どう思う?", "お願いします", "それでいいよ",
                     "面白かった", "楽しみだなあ、うん、そうしよう。"):
            self.assertIn(endpoint_hint(text), ("final", "neutral"), text)
        self.assertEqual(endpoint_hint("どう思う?"), "final")
        self.assertEqual(endpoint_hint("今日は暑いです"), "final")

    def test_neutral_and_empty(self):
        self.assertEqual(endpoint_hint(""), "neutral")
        self.assertEqual(endpoint_hint("りんご"), "neutral")


class ControlLayerTests(unittest.TestCase):
    """フェーズ7: ルールベース会話制御層。"""

    def test_listener_backchannel_never_speaks_over_user(self):
        c = ConversationControlLayer(rng=lambda: 0.0)
        # 相槌は発話確定後の本応答へ統合し、ユーザー発話中には重ねない。
        self.assertIsNone(c.listener_backchannel(speaking_s=1.0, silence_s=0.6, now=100))
        self.assertIsNone(c.listener_backchannel(speaking_s=5.0, silence_s=0.1, now=100))
        self.assertIsNone(c.listener_backchannel(speaking_s=5.0, silence_s=2.0, now=100))
        self.assertFalse(c.should_listen_ack(speaking_s=20.0, silence_s=0.4, now=200))

    def test_template_thinking_filler_is_disabled(self):
        c = ConversationControlLayer(rng=lambda: 0.0)
        self.assertIsNone(c.thinking_filler(question=False, text_len=5, now=100))  # 短い発話
        self.assertIsNone(c.thinking_filler(question=True, text_len=5, now=100))
        self.assertIsNone(c.thinking_filler(question=True, text_len=30, now=105))
        self.assertIsNone(c.thinking_filler(question=False, text_len=30, now=121))

    def test_disabled_layers_return_none(self):
        c = ConversationControlLayer(listener_enabled=False, filler_enabled=False,
                                     rng=lambda: 0.0)
        self.assertIsNone(c.listener_backchannel(speaking_s=9.0, silence_s=0.6, now=100))
        self.assertIsNone(c.thinking_filler(question=True, text_len=30, now=100))


if __name__ == "__main__":
    unittest.main()
