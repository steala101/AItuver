"""faster-whisper による STT 実装 (日英対応)。"""
from __future__ import annotations

import logging
import re
import time

import numpy as np

from neuro_voice.stt.base import Transcriber
from neuro_voice.stt.transcript import Transcript, collect_words

logger = logging.getLogger(__name__)


class FasterWhisperSTT(Transcriber):
    """large-v3-turbo (GPU) / 軽量モデル (CPU) による低遅延音声認識。"""

    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str | None = None,
        download_root: str = "models",
        cpu_threads: int = 8,
        cpu_model: str | None = None,
        beam_size: int = 5,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        corrections: dict | None = None,
        wake_word_aliases: dict | None = None,
        partial_beam_size: int | None = None,
        final_beam_size: int | None = None,
        word_confidence: bool = True,
        temperature_fallback: bool = True,
        repetition_penalty: float = 1.0,
        no_repeat_ngram_size: int = 0,
        keep_fillers: bool = True,
    ):
        from faster_whisper import WhisperModel

        # 認識精度まわりの設定
        self._beam_size = max(1, min(10, int(beam_size)))
        # 確定認識は精度優先、途中認識(割り込み語検出用)は速度優先で beam を分ける。
        self._final_beam_size = max(1, min(10, int(
            final_beam_size if final_beam_size is not None else self._beam_size)))
        self._partial_beam_size = max(1, min(10, int(
            partial_beam_size if partial_beam_size is not None else 1)))
        self._initial_prompt = (initial_prompt or "").strip() or None
        self._hotwords = (hotwords or "").strip() or None
        # Refreshed each turn from the live conversation; falls back to config.
        self._context_prompt: str | None = None
        self._context_hotwords: str | None = None
        self._word_confidence = bool(word_confidence)
        # Whisper retries a segment at a higher temperature when the decode
        # looks degenerate (compression ratio / logprob).  Pinning 0.0 removes
        # that safety net entirely, which is how a stutter loop reaches the UI.
        # Stop at 0.4: higher temperatures sample nearly at random and trade a
        # stutter for a wrong word.  This ladder only runs on a segment Whisper
        # has already judged degenerate.
        self._temperatures = (0.0, 0.2, 0.4) if temperature_fallback else 0.0
        self._repetition_penalty = max(1.0, float(repetition_penalty))
        self._no_repeat_ngram_size = max(0, int(no_repeat_ngram_size))
        # Whisper is trained to produce tidy captions and drops 「えーと」「うん」
        # on its own.  Disabling the default token suppression keeps the
        # hesitation audible to the assistant, which is part of how a person
        # actually sounds.
        self._keep_fillers = bool(keep_fillers)
        # 誤変換の置換辞書 (誤 → 正)。認識後に文字列置換で補正する。
        self._corrections = {
            str(k): str(v) for k, v in (corrections or {}).items() if str(k)
        }
        # 呼びかけ語の別認識は、発話先頭かつ区切りのある短い呼びかけだけ補正する。
        # 一般語（例: 「こっち」）を文中で置換して意味を壊さないための制限。
        self._wake_word_aliases = {
            str(canonical).strip(): [str(alias).strip() for alias in aliases if str(alias).strip()]
            for canonical, aliases in (wake_word_aliases or {}).items()
            if str(canonical).strip() and isinstance(aliases, (list, tuple))
        }

        # CPUでは large-v3-turbo は重すぎるため、指定があれば軽量モデルを使う。
        # 未指定なら従来どおり model をそのまま使用する。
        self._gpu_model = model
        self._cpu_model = cpu_model or model

        def _load(dev: str):
            name = self._cpu_model if dev == "cpu" else self._gpu_model
            ct = "int8" if dev == "cpu" else compute_type
            m = WhisperModel(
                name,
                device=dev,
                compute_type=ct,
                download_root=download_root,
                cpu_threads=cpu_threads,
            )
            return m, name

        start = time.perf_counter()
        try:
            self._model, used = _load(device)
        except Exception:
            if not device.startswith("cuda"):
                raise
            # VRAM不足等でGPU初期化に失敗した場合はCPUへフォールバック
            logger.warning("GPUでのSTT初期化に失敗。CPU (int8) にフォールバックします")
            device = "cpu"
            self._model, used = _load("cpu")
        self._language = language
        self.device = device
        self.model_name = used
        logger.info(
            "faster-whisper '%s' ロード完了 (%.1fs, device=%s)",
            used, time.perf_counter() - start, device,
        )
        self._warmup()

    def _warmup(self) -> None:
        """ダミー音声で1回推論し、CUDA/cuDNNカーネル初期化を済ませる。

        これをしないと最初の発話の認識が数秒遅れたり、
        取りこぼしたように見えることがある (初回発話対策)。
        """
        try:
            t0 = time.perf_counter()
            segments, _ = self._model.transcribe(
                np.zeros(8000, dtype=np.float32),  # 0.5秒の無音
                language=self._language or "ja",
                beam_size=1,
                without_timestamps=True,
            )
            list(segments)  # 遅延評価のため必ず消費する
            logger.info("STTウォームアップ完了 (%.1fs)", time.perf_counter() - t0)
        except Exception:
            logger.warning("STTウォームアップに失敗 (続行します)", exc_info=True)

    def set_context_bias(self, initial_prompt: str = "", hotwords: str = "") -> None:
        """Bias the *next* decode toward a few verified proper nouns.

        Deliberately conservative.  Whisper emits ``initial_prompt`` content
        into the transcript and inserts hotwords into unrelated speech, so this
        must receive a short, stable list — never free text harvested from
        earlier transcripts, which would make a mis-recognition self-reinforcing.
        """
        self._context_prompt = str(initial_prompt or "").strip() or None
        self._context_hotwords = str(hotwords or "").strip() or None

    def _decode_prompt(self) -> str | None:
        return self._context_prompt or self._initial_prompt

    def _decode_hotwords(self) -> str | None:
        return self._context_hotwords or self._hotwords

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """確定認識 (発話終了後)。精度優先の beam を使う。"""
        return self._run(audio, self._final_beam_size).text

    def transcribe_partial(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """途中認識 (割り込み語検出用)。速度優先の小さい beam を使う。"""
        # Word timestamps cost time and the interim pass only feeds keyword
        # interruption detection, which does not care about confidence.
        return self._run(audio, self._partial_beam_size, with_words=False).text

    def transcribe_detailed(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        """確定認識を、語ごとの確信度を保ったまま返す。"""
        return self._run(audio, self._final_beam_size)

    def _run(
        self, audio: np.ndarray, beam_size: int, *, with_words: bool = True,
    ) -> Transcript:
        # beam_size を上げると候補を広く探索し、同音の取り違え
        # (例: おみず→おすい) を減らせる。initial_prompt / hotwords で
        # 語彙のバイアスをかけ、固有名詞や口語を拾いやすくする。
        kw = dict(
            language=self._language,
            beam_size=beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=not with_words,
            # A single pinned temperature disables Whisper's own fallback, so a
            # decode that starts looping ("最初は、最初は、最初は") has no way to
            # recover.  Keeping the ladder lets a failed segment be retried.
            temperature=self._temperatures,
            initial_prompt=self._decode_prompt(),
        )
        if with_words and self._word_confidence:
            # Per-word probability is the only reliable signal for "this
            # specific word is probably the wrong homophone".
            kw["word_timestamps"] = True
        hotwords = self._decode_hotwords()
        if hotwords:
            kw["hotwords"] = hotwords  # faster-whisper 1.0.2+ のみ対応
        if self._keep_fillers:
            # The default (-1) suppresses a set of tokens that biases output
            # toward clean written text.  An empty list transcribes what was
            # said, including the hesitation at the start of a sentence.
            kw["suppress_tokens"] = []
        if self._repetition_penalty > 1.0:
            kw["repetition_penalty"] = self._repetition_penalty
        if self._no_repeat_ngram_size > 0:
            kw["no_repeat_ngram_size"] = self._no_repeat_ngram_size
        try:
            segments, info = self._model.transcribe(audio, **kw)
            text, words, avg_logprob, no_speech, compression = collect_words(segments)
        except TypeError:
            # 古い faster-whisper は hotwords / word_timestamps / repetition_penalty
            # 未対応 → 外して再試行
            for key in ("hotwords", "word_timestamps", "repetition_penalty",
                        "no_repeat_ngram_size"):
                kw.pop(key, None)
            kw["without_timestamps"] = True
            segments, info = self._model.transcribe(audio, **kw)
            text, words, avg_logprob, no_speech, compression = collect_words(segments)
        repaired = self._apply_wake_word_aliases(self._apply_corrections(text))
        return Transcript(
            text=repaired,
            language=str(getattr(info, "language", "") or self._language or ""),
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech,
            compression_ratio=compression,
            # Alias/correction rewriting can shift surfaces, so word offsets are
            # only kept when the text came through untouched.
            words=words if repaired == text else [],
        )

    def _apply_corrections(self, text: str) -> str:
        """既知の誤変換を置換辞書で補正する (誤 → 正)。"""
        if not text or not self._corrections:
            return text
        for wrong, right in self._corrections.items():
            if wrong and wrong in text:
                text = text.replace(wrong, right)
        return text

    def _apply_wake_word_aliases(self, text: str) -> str:
        """呼びかけ位置だけ、設定した聞き間違い候補を正規名へ戻す。"""
        if not text or not self._wake_word_aliases:
            return text
        leading = len(text) - len(text.lstrip())
        prefix, body = text[:leading], text[leading:]
        for canonical, aliases in self._wake_word_aliases.items():
            for alias in aliases:
                # 区切りがある「こっち、…」「おち こんにちは」だけを補正する。
                pattern = re.compile(
                    rf"^{re.escape(alias)}(?=$|[\s、,。.!！?？])", re.IGNORECASE
                )
                if pattern.search(body):
                    corrected = pattern.sub(canonical, body, count=1)
                    logger.info("STT呼びかけ補正: %r → %r", body[:30], corrected[:30])
                    return prefix + corrected
        return text
