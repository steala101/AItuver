"""疑似フルデュプレックス用の判定ロジック (純粋関数・テスト可能)。

フェーズ2: 明確な割り込み語の検出。
- interim (STT途中結果) では強い語だけで即ハード割り込み (誤停止を抑える)
- final (確定STT) ではやや広い語彙も割り込みとして扱い、
  「いや」「違う」等が相づち/誤検知として飲み込まれるのを防ぐ

フェーズ5: 音響エコーガード。
- Bot送信TTSのエネルギー包絡をリングバッファに保持し、
  受信したユーザー音声の包絡と相互相関を取る。スピーカー回り込み
  (=AI自身の声がユーザーのマイクへ戻ってきた音) は包絡が高く相関する。
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata

import numpy as np

# interim (発話確定前) でも即断してよい強い割り込み語。
# 相づち語 (うん/はい/へえ…) と衝突しないものだけを載せること。
# STTは漢字で確定することが多いため、ひらがな・漢字の両表記を載せる
# (_normalize はカタカナ・全半角・空白のみ吸収し、漢字はそのまま)。
_HARD_IMMEDIATE = (
    "まって", "待って", "ちょっとまって", "ちょっと待って", "まてまて", "待て待て",
    "とめて", "止めて", "やめて", "止めろ", "すとっぷ",
    "だまって", "黙って", "しずかに", "静かに",
    "いったんとめ", "一旦止め", "いったんやめ", "一旦やめ",
)

# 確定STTでのみ割り込み扱いする語 (interimで使うには誤認リスクが高い)。
_HARD_FINAL = (
    "ちがう", "違う", "ちがうちがう", "違う違う", "ちがくて", "違くて",
    "いやいや", "いやちがう", "いや違う",
    "そうじゃなくて", "それじゃなくて", "そうじゃない", "それはいい",
    "ごめんていせい", "ごめん訂正", "ていせい", "訂正", "もういい",
)

_STRIP = re.compile(r"[\s　、。,.!?！？〜～・…「」『』()（）]+")


def _normalize(text: str) -> str:
    """NFKC → カタカナ→ひらがな → 記号/空白除去。STTの表記揺れを吸収する。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in s)
    return _STRIP.sub("", s).lower()


class InterruptKeywordDetector:
    """明確な割り込み語の検出器。文脈 (AI発話中) 側でゲートして使うこと。"""

    def __init__(self, extra_words: list[str] | None = None):
        extra = tuple(_normalize(w) for w in (extra_words or []) if str(w).strip())
        self._immediate = tuple(_normalize(w) for w in _HARD_IMMEDIATE) + extra
        self._final = tuple(_normalize(w) for w in _HARD_FINAL)

    def is_hard_interrupt(self, text: str, *, final: bool = False) -> bool:
        n = _normalize(text)
        if not n:
            return False
        # 長い文は通常の実質発話。既存の分類 (substantive) に任せる
        if len(n) > 20:
            return False
        if any(word in n for word in self._immediate):
            return True
        if final:
            # final限定語は「短い発話がその語で始まる/その語そのもの」に限定し、
            # 「いや、それでさ…」のような接頭の相づちを誤検出しない
            return any(n == word or (n.startswith(word) and len(n) <= len(word) + 4)
                       for word in self._final)
        return False


# ---------- フェーズ6: 発話終了判定のヒント ----------

# 途中結果がこれらで終わる = 続きを言おうとしている可能性が高い (終了判定を延長)
_CONTINUATION_TAILS = (
    "けど", "けれど", "だけど", "ですが", "が", "で", "でも", "から", "ので",
    "のに", "し", "て", "って", "とか", "たとえば", "例えば", "あと", "それで",
    "そして", "それから", "えっと", "ええと", "えーと", "うーん", "うんと",
    "なんか", "というか", "つまり", "は", "も", "を", "に",
)
# これらで終わる = 文が完結している可能性が高い (終了判定を短縮してよい)
_FINAL_TAILS = (
    "か", "ですか", "ますか", "かな", "かね", "だよね", "よね", "ね", "よ",
    "です", "ます", "でした", "ました", "だ", "た", "だった", "じゃん",
    "でしょ", "だろう", "お願い", "ください",
)


def endpoint_hint(partial_text: str) -> str:
    """STT途中結果から発話終了の見込みを返す: "continue" / "final" / "neutral"。"""
    s = unicodedata.normalize("NFKC", str(partial_text or "")).strip()
    s = s.rstrip("、,。.…その他 　")
    if not s:
        return "neutral"
    if s.endswith(("?", "？", "!", "！")):
        return "final"
    # 長い一致を優先 (「ですが」は continue、「です」は final)
    for tail in sorted(_CONTINUATION_TAILS, key=len, reverse=True):
        if s.endswith(tail):
            # 1文字助詞は直前が漢字/かなに関わらず誤爆しやすいので4文字以上の発話に限定
            if len(tail) == 1 and len(s) < 4:
                break
            return "continue"
    for tail in sorted(_FINAL_TAILS, key=len, reverse=True):
        if s.endswith(tail):
            return "final"
    return "neutral"


# ---------- フェーズ7: ルールベース会話制御層 ----------

class ConversationControlLayer:
    """軽量な会話制御 (初期実装はルールベース。将来小型LLMへ差し替え可能)。

    ユーザー発話中の相槌と定型のthinking fillerは廃止済み。相槌は発話確定後、
    Conversation Plannerが本応答の冒頭として必要なときだけ生成する。公開メソッドは
    古い呼び出しとの互換性のため残すが、音声を発生させる判断は返さない。
    """

    def __init__(self, *, listener_enabled: bool = True, listener_cooldown_s: float = 8.0,
                 filler_enabled: bool = True, filler_cooldown_s: float = 20.0,
                 ack_pool=None, filler_pool=None,
                 contextual_enabled: bool = True, contextual_min_chars: int = 12,
                 contextual_cooldown_s: float = 18.0,
                 rng=None):
        self._contextual_min_chars = max(4, int(contextual_min_chars))

    def wants_contextual_ack(self, partial_text: str, *, now: float | None = None) -> bool:
        """相づちをLLMに文脈依存で作らせるべきか (ルール相づちの代わりに)。

        listener_backchannel が相づちを打つと決めた直後に bot が呼ぶ。長め発話で、
        LLM相づちのクールダウンを過ぎていれば True。False ならルール相づちを使う。
        """
        return False

    @property
    def min_ack_chars(self) -> int:
        return self._contextual_min_chars

    def should_listen_ack(self, *, speaking_s: float, silence_s: float,
                          now: float | None = None) -> bool:
        """ユーザー発話中にはAI音声を重ねない。常にFalse。"""
        return False

    def listener_backchannel(self, *args, **kwargs) -> None:
        """旧API互換。途中相槌は行わないため常にNone。"""
        return None

    def thinking_filler(self, *args, **kwargs) -> None:
        """繋ぎ発話(テンプレート)は廃止。常に None (=何も言わない)。

        「えっと」等の定型句は文脈と無関係で違和感があるため使わない。応答生成前の
        間はそのまま黙って待つ。文脈に合う一言はLLMの本応答冒頭に自然に含める。
        """
        return None


class AcousticEchoGuard:
    """Bot送信TTSとの包絡相関で、スピーカー回り込みを判定する (フェーズ5)。

    生波形の相関は遅延・コーデック・音量で崩れるため、20ms RMS包絡 (50Hz)
    どうしのピアソン相関の最大値 (全オフセット走査) を使う。軽量 (数ms) で、
    発話のリズム・抑揚パターンが一致するかを見る現実的なガード。
    """

    def __init__(self, window_s: float = 6.0, hop_s: float = 0.02):
        self._hop_s = hop_s
        self._max_frames = max(50, int(window_s / hop_s))
        self._ref = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()

    @staticmethod
    def _envelope(wav: np.ndarray, sr: int, hop_s: float) -> np.ndarray:
        wav = np.asarray(wav, dtype=np.float32)
        hop = max(1, int(sr * hop_s))
        n = len(wav) // hop
        if n < 1:
            return np.zeros(0, dtype=np.float32)
        frames = wav[: n * hop].reshape(n, hop)
        rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-9)
        return np.log1p(rms * 50.0).astype(np.float32)  # 音量差に鈍感な対数圧縮

    def add_reference(self, wav: np.ndarray, sr: int) -> None:
        """送信するTTS音声を参照バッファへ追加する (合成完了時に呼ぶ)。"""
        env = self._envelope(wav, sr, self._hop_s)
        if len(env) == 0:
            return
        with self._lock:
            self._ref = np.concatenate([self._ref, env])[-self._max_frames:]

    def clear(self) -> None:
        with self._lock:
            self._ref = np.zeros(0, dtype=np.float32)

    def score(self, wav: np.ndarray, sr: int) -> float:
        """受信音声と参照バッファの最大包絡相関 (0.0〜1.0)。判定不能は0.0。"""
        with self._lock:
            ref = self._ref.copy()
        query = self._envelope(wav, sr, self._hop_s)[:150]  # 先頭3秒まで
        if len(query) < 15 or len(ref) < len(query):  # 0.3秒未満は判定しない
            return 0.0
        q = query - float(np.mean(query))
        q_std = float(np.std(q))
        if q_std < 1e-6:
            return 0.0
        q /= q_std
        best = 0.0
        # 50Hz包絡なのでオフセット全走査でも高々数万積和 (数ms)
        for offset in range(0, len(ref) - len(query) + 1):
            window = ref[offset: offset + len(query)]
            w = window - float(np.mean(window))
            w_std = float(np.std(w))
            if w_std < 1e-6:
                continue
            corr = float(np.dot(q, w / w_std)) / len(q)
            if corr > best:
                best = corr
        return max(0.0, min(1.0, best))
