"""システム音声(Discord通話)のループバック取り込み。

Discord は 2026-03 以降、通話を E2EE(DAVE) で強制暗号化するため、Bot が
voice-recv で受信した音声はもう復号できない。そこで「PCがDiscordから鳴らして
いる音声(=Discordクライアントが復号して再生済みの音)」をループバックで取り込み、
STT へ渡す。

ループバック取り込みには **soundcard** ライブラリを使う。python-sounddevice は
WASAPIループバック非対応(WasapiSettingsにloopback指定がない)なので使えない。
soundcard は WASAPI ループバックに対応している。

注意:
- soundcard は Windows/WASAPI で「1チャンネルだけ録音するとノイズになる」既知バグが
  あるため、必ずステレオ(全チャンネル)で録ってから自前でモノラル化する。
- ループバックには自分(chibi)の声は含まれない(Discordは自分の声を自分へ返さない)。
  本人の声もニューロに聞かせたい場合は mix_mic=True でマイクと合成する。
- ニューロのTTSはVCへ送られ、Discordクライアントが再生 → ループバックにも乗る。
  自己音声の二重認識を防ぐため、発話中は呼び出し側で feedback_muted=True にすること。

MicCapture と同じく out_queue へ 16k mono float32 フレームを供給する。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)


# `list_output_devices` の旧実装がここにあったが、136行目の同名関数に
# 完全に隠れており一度も実行されていなかった（後から定義された方が勝つ）。
# 旧実装は id にスピーカー名を入れており、表示名が同じ機器を区別できない。
# 現行版は endpoint ID を使う。編集しても効果が出ない罠なので削除した。



def _value(device, *names: str):
    """Read device metadata across soundcard versions without assumptions."""
    for name in names:
        value = getattr(device, name, None)
        if value not in (None, ""):
            return value
    return None


def _endpoint_id(device) -> str:
    return str(_value(device, "id", "endpoint_id", "_id") or "")


def _device_info(device, *, default: bool = False) -> dict:
    endpoint_id = _endpoint_id(device)
    return {
        "id": endpoint_id,
        "endpoint_id": endpoint_id,
        "name": str(_value(device, "name") or ""),
        "isloopback": bool(_value(device, "isloopback", "is_loopback") or False),
        "channels": _value(device, "channels"),
        "samplerate": _value(device, "samplerate", "sample_rate"),
        "default": bool(default),
    }


def _find_loopback_microphone(speaker, microphones):
    """Resolve strictly by endpoint/device ID, never by a name substring."""
    speaker_id = _endpoint_id(speaker)
    if not speaker_id:
        return None
    for microphone in microphones:
        if not bool(_value(microphone, "isloopback", "is_loopback") or False):
            continue
        if _endpoint_id(microphone) == speaker_id:
            return microphone
    return None


def soundcard_inventory() -> dict:
    """Return outputs and the subset that have an exact loopback endpoint."""
    empty = {"outputs": [], "loopback_outputs": [], "microphones": [], "default_output_id": ""}
    try:
        import soundcard as sc
    except Exception as exc:
        logger.warning("soundcard inventory unavailable: %s", exc)
        return empty
    try:
        speakers = list(sc.all_speakers())
        microphones = list(sc.all_microphones(include_loopback=True))
        default = sc.default_speaker()
        default_id = _endpoint_id(default) if default is not None else ""
        outputs, loopback_outputs = [], []
        for speaker in speakers:
            row = _device_info(speaker, default=_endpoint_id(speaker) == default_id)
            loopback = _find_loopback_microphone(speaker, microphones)
            row["loopback_available"] = loopback is not None
            row["loopback_microphone_id"] = _endpoint_id(loopback) if loopback else ""
            outputs.append(row)
            if loopback is not None:
                loopback_outputs.append(row.copy())
        inventory = {
            "outputs": outputs,
            "loopback_outputs": loopback_outputs,
            "microphones": [_device_info(mic) for mic in microphones],
            "default_output_id": default_id,
        }
        logger.info("soundcard.all_speakers(): %r", speakers)
        logger.info("soundcard.all_microphones(include_loopback=True): %r", microphones)
        logger.info("soundcard endpoint inventory: %s", inventory)
        return inventory
    except Exception:
        logger.exception("soundcard endpoint inventory failed")
        return empty


def list_output_devices() -> list[dict]:
    """Compatibility API: values are endpoint IDs, never ambiguous display names."""
    return [
        {
            "id": row["id"], "name": row["name"], "default": row["default"],
            "loopback_available": row["loopback_available"],
        }
        for row in soundcard_inventory()["outputs"]
    ]


class LoopbackCapture:
    """soundcard の WASAPI ループバックで出力音声を取り込み、16k mono を供給する。

    mix_mic=True のときは本人マイク(sounddevice)も開き、マイクを基準クロックとして
    ループバック音声を足し込む(本人+通話相手 両方を聞ける)。
    """

    def __init__(
        self,
        out_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        sample_rate: int = 16000,
        frame_samples: int = 512,
        loopback_device: str | None = None,
        mic_device: int | str | None = None,
        mix_mic: bool = False,
    ):
        self._q = out_queue
        self._loop = loop
        self._sr = sample_rate
        self._frame = frame_samples
        self._loopback_device = loopback_device  # スピーカー名(部分一致) or None=既定
        self._mic_device = mic_device
        self._mix_mic = mix_mic
        self.muted = False           # 全体ミュート (取り込み停止)
        self.feedback_muted = False  # ニューロ発話中の自動ミュート(自己音声の二重認識防止)
        self.mic_muted = False       # 本人マイク成分だけミュート (通話相手の声は聞く)

        self._sc = None
        self._speaker = None
        self._loopback_mic = None
        self._recorder = None        # soundcard recorder (context enter済み)
        self.last_data = 0.0         # 最後にrecordが返った時刻 (死活監視用)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mic_stream = None
        self._ring = np.zeros(0, dtype=np.float32)
        self._ring_lock = threading.Lock()
        self._rms_started_at = 0.0
        self._rms_sum_squares = 0.0
        self._rms_samples = 0
        self._rms_reported = False

    # ---------- 起動/停止 ----------

    def start(self) -> None:
        self._stop.clear()  # 再起動 (stop→start) できるように必ずリセット
        self.last_data = time.monotonic()
        try:
            import soundcard as sc
        except Exception as e:
            raise RuntimeError(
                "ループバック取り込みには soundcard が必要です。"
                'py -m pip install soundcard で導入してください'
                f" (詳細: {e})"
            ) from e
        self._sc = sc
        # デバイス解決とレコーダ確保は同期的に行い、失敗をここで送出する
        spk = self._resolve_speaker()
        speakers = list(sc.all_speakers())
        microphones = list(sc.all_microphones(include_loopback=True))
        mic = _find_loopback_microphone(spk, microphones)
        self._speaker = spk
        self._loopback_mic = mic
        logger.info("Loopback selected config=%r resolved speaker=%s", self._loopback_device, _device_info(spk))
        logger.info("soundcard.all_speakers(): %r", speakers)
        logger.info("soundcard.all_microphones(include_loopback=True): %r", microphones)
        logger.info("Loopback available microphones=%s", [_device_info(item) for item in microphones])
        if mic is None:
            raise RuntimeError(
                "この出力デバイスはループバック取得不可です。"
                " speaker endpoint ID と一致する loopback microphone が見つかりません: "
                f"{_device_info(spk)}"
            )
        logger.info("Loopback matched microphone=%s", _device_info(mic))
        # blocksize は numframes より大きめ(WASAPIのバッファアンダーラン対策)
        self._recorder = mic.recorder(samplerate=self._sr, blocksize=self._frame * 4)
        self._recorder.__enter__()
        logger.info("ループバック取り込み開始 (soundcard, speaker='%s', mix_mic=%s)",
                    spk.name, self._mix_mic)

        self._rms_started_at = time.monotonic()
        self._rms_sum_squares = 0.0
        self._rms_samples = 0
        self._rms_reported = False
        self._thread = threading.Thread(target=self._loopback_run, daemon=True,
                                        name="loopback-rec")
        self._thread.start()

        if self._mix_mic:
            import sounddevice as sd

            self._mic_stream = sd.InputStream(
                samplerate=self._sr, blocksize=self._frame, channels=1,
                dtype="float32", device=self._mic_device,
                callback=self._mic_clock_callback,
            )
            self._mic_stream.start()
            logger.info("本人マイクをミックス取り込み (device=%s)", self._mic_device)

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(timeout=1.5)
        self._thread = None
        if self._recorder is not None:
            try:
                self._recorder.__exit__(None, None, None)
            except Exception:
                logger.debug("recorder終了でエラー", exc_info=True)
            self._recorder = None
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                logger.debug("micストリーム停止でエラー", exc_info=True)
            self._mic_stream = None
        logger.info("ループバック取り込み停止")

    # ---------- 内部 ----------

    def _resolve_speaker_legacy(self):
        sc = self._sc
        dev = self._loopback_device
        if dev in (None, "", "null"):
            spk = sc.default_speaker()
            if spk is None:
                raise RuntimeError("既定の出力スピーカーが見つかりません")
            return spk
        try:
            return sc.get_speaker(str(dev))  # 名前の部分一致
        except Exception:
            spk = sc.default_speaker()
            if spk is None:
                raise RuntimeError(f"スピーカーが見つかりません: {dev}")
            return spk

    def _resolve_speaker(self):
        """Resolve selected endpoint exactly; display names are legacy-only exact matches."""
        sc = self._sc
        requested = self._loopback_device
        if requested in (None, "", "null"):
            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("既定の出力スピーカーが見つかりません")
            return speaker
        requested = str(requested)
        speakers = list(sc.all_speakers())
        for speaker in speakers:
            if _endpoint_id(speaker) == requested:
                return speaker
        for speaker in speakers:
            if str(getattr(speaker, "name", "")) == requested:
                logger.warning("Legacy speaker name used for loopback; save settings to migrate to endpoint ID: %r", requested)
                return speaker
        raise RuntimeError(f"指定した出力 endpoint ID が見つかりません: {requested}")

    def _observe_startup_rms(self, pcm: np.ndarray) -> None:
        """Warn once when the first three seconds contain only silence."""
        if self._rms_reported or self._rms_started_at <= 0:
            return
        samples = np.asarray(pcm, dtype=np.float32).reshape(-1)
        self._rms_sum_squares += float(np.dot(samples, samples))
        self._rms_samples += int(samples.size)
        elapsed = time.monotonic() - self._rms_started_at
        if elapsed < 3.0:
            return
        self._rms_reported = True
        rms = (self._rms_sum_squares / max(1, self._rms_samples)) ** 0.5
        threshold = 0.0005
        if rms < threshold:
            logger.warning(
                "Loopback PCM was silent during startup: rms=%.7f over %.1fs. "
                "Verify Discord output is routed to the selected virtual output endpoint.",
                rms, elapsed,
            )
        else:
            logger.info("Loopback startup PCM RMS: %.7f over %.1fs", rms, elapsed)

    def _loopback_run(self) -> None:
        """録音スレッド。soundcardのrecordはブロッキングなので別スレッドで回す。"""
        emit_buf = np.zeros(0, dtype=np.float32)
        try:
            while not self._stop.is_set():
                data = self._recorder.record(numframes=self._frame)  # (n, ch) 16k float32
                self.last_data = time.monotonic()  # 死活監視 (ミュート中でも更新)
                self._observe_startup_rms(data)
                if self.muted or self.feedback_muted:
                    emit_buf = np.zeros(0, dtype=np.float32)
                    if self._mix_mic:
                        with self._ring_lock:
                            self._ring = np.zeros(0, dtype=np.float32)
                    continue
                # 1chだけ録るとsoundcardがノイズ化するため必ず全ch→平均でモノラル化
                mono = data.mean(axis=1) if data.ndim > 1 else data
                mono = np.ascontiguousarray(mono, dtype=np.float32)
                if self._mix_mic:
                    with self._ring_lock:
                        self._ring = np.concatenate((self._ring, mono))
                        cap = self._sr * 2  # 2秒上限(暴走防止)
                        if len(self._ring) > cap:
                            self._ring = self._ring[-cap:]
                else:
                    emit_buf = np.concatenate((emit_buf, mono))
                    while len(emit_buf) >= self._frame:
                        self._push(emit_buf[:self._frame].copy(), "loopback")
                        emit_buf = emit_buf[self._frame:]
        except Exception:
            if not self._stop.is_set():
                logger.exception("ループバック録音スレッドでエラー")

    def _mic_clock_callback(self, indata: np.ndarray, frames, time_info, status) -> None:
        """本人マイクを基準クロックに、マイク(本人)と通話ループバック(他人)を
        *別々に* 供給する。足し算すると声紋が混ざって話者分離できないため、
        それぞれ独立したクリーンな音声として流し、bot側で個別に声紋識別する。"""
        if status:
            logger.debug("mic(mix) status: %s", status)
        if self.muted:
            return
        try:
            mic = indata[:, 0].astype(np.float32, copy=True)
            n = len(mic)
            with self._ring_lock:
                if len(self._ring) >= n:
                    lb = self._ring[:n].copy()
                    self._ring = self._ring[n:]
                else:
                    lb = np.zeros(n, dtype=np.float32)
                    if len(self._ring):
                        lb[:len(self._ring)] = self._ring
                        self._ring = np.zeros(0, dtype=np.float32)
            # 本人マイク(=チビ) を単独で流す (mic_mutedなら流さない)
            if not self.mic_muted:
                self._push(mic, "mic")
            # 通話相手(ループバック) を単独で流す。ニューロ発話中(feedback)は
            # 自分のTTSが乗るので流さない
            if not self.feedback_muted:
                self._push(lb, "loopback")
        except Exception:
            logger.exception("ミックスコールバックでエラー")

    def _push(self, frame: np.ndarray, source: str = "loopback") -> None:
        # 話者分離のため、どの音源(mic=本人 / loopback=通話相手)かを付けて流す
        item = (source, frame)
        def _put(x=item) -> None:
            if not self._q.full():
                self._q.put_nowait(x)
        try:
            self._loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # ループ終了後のコールバックは無視
