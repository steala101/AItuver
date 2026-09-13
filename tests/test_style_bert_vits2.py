"""StyleBertVits2TTS (スタイル写像・合成パラメータ・音量ゲイン) のテスト。

サーバー不要: HTTPセッションをフェイクに差し替えてリクエスト内容を検証する。
"""
from __future__ import annotations

import importlib.util as _ilu
import io
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    """パッケージ__init__ (Python3.11依存) を経由せずモジュールを単独ロードする。"""
    spec = _ilu.spec_from_file_location(name, _ROOT / rel)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    from neuro_voice.tts.style_bert_vits2 import StyleBertVits2TTS
    from neuro_voice.tts.style import StyleManager
except ImportError:
    # 依存モジュールを neuro_voice.* の名前で登録してから対象をロード
    for mod_name, rel in [
        ("neuro_voice", None),
        ("neuro_voice.utils", None),
        ("neuro_voice.tts", None),
    ]:
        if mod_name not in sys.modules:
            pkg = types.ModuleType(mod_name)
            pkg.__path__ = []
            sys.modules[mod_name] = pkg
    _load("neuro_voice.utils.emotion", "neuro_voice/utils/emotion.py")
    _load("neuro_voice.tts.base", "neuro_voice/tts/base.py")
    _load("neuro_voice.tts.style", "neuro_voice/tts/style.py")
    _load("neuro_voice.tts.pronunciation", "neuro_voice/tts/pronunciation.py")
    mod = _load("neuro_voice.tts.style_bert_vits2", "neuro_voice/tts/style_bert_vits2.py")
    StyleBertVits2TTS = mod.StyleBertVits2TTS
    StyleManager = sys.modules["neuro_voice.tts.style"].StyleManager


class _FakeResponse:
    def __init__(self, content: bytes = b"", json_data=None, status: int = 200):
        self.content = content
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _wav_bytes(seconds: float = 0.1, sr: int = 44100, amp: float = 0.5) -> bytes:
    buf = io.BytesIO()
    data = np.full(int(sr * seconds), amp, dtype=np.float32)
    sf.write(buf, data, sr, format="WAV")
    return buf.getvalue()


class _FakeSession:
    """/models/info と /voice に応答するフェイク。最後の /voice パラメータを記録。"""

    def __init__(self, styles=("Neutral", "ノーマル", "るんるん", "よふかし")):
        self.last_params = None
        self._info = {
            "0": {
                "config_path": "model_assets/amitaro/config.json",
                "spk2id": {"あみたろ": 0},
                "style2id": {s: i for i, s in enumerate(styles)},
            }
        }

    def get(self, url, params=None, timeout=None):
        if "/models/info" in url:
            return _FakeResponse(json_data=self._info)
        if "/voice" in url:
            self.last_params = dict(params)
            return _FakeResponse(content=_wav_bytes())
        raise AssertionError(url)

    def close(self):
        pass


def _make_tts(**kw) -> tuple[StyleBertVits2TTS, _FakeSession]:
    kw.setdefault("auto_launch", False)
    kw.setdefault("model_name", "amitaro")
    kw.setdefault("emotion_styles", {"joy": "るんるん", "sad": "よふかし"})
    kw.setdefault("delivery_styles", {"calm": "よふかし"})
    kw.setdefault("default_style", "ノーマル")
    tts = StyleBertVits2TTS(**kw)
    fake = _FakeSession()
    tts._session = fake
    tts._load_model_info()
    return tts, fake


def test_emotion_maps_to_model_style():
    tts, fake = _make_tts()
    tts.synthesize("こんにちは", emotion="joy")
    assert fake.last_params["style"] == "るんるん"
    assert fake.last_params["model_name"] == "amitaro"
    assert fake.last_params["language"] == "JP"


def test_unknown_style_falls_back():
    tts, fake = _make_tts(emotion_styles={"angry": "存在しないスタイル"})
    tts.synthesize("むむ", emotion="angry")
    # モデルに無いスタイルは既定 (ノーマル) へフォールバック
    assert fake.last_params["style"] == "ノーマル"


def test_delivery_overrides_emotion():
    tts, fake = _make_tts()
    style = StyleManager().resolve("joy", "calm")
    tts.synthesize("おやすみ", emotion="joy", style=style)
    assert fake.last_params["style"] == "よふかし"


def test_length_is_inverse_of_speed():
    tts, fake = _make_tts(speed=1.0)
    style = StyleManager().resolve("joy", "lively")  # speed > 1
    tts.synthesize("はやい", style=style)
    assert fake.last_params["length"] < 1.0  # 速い = length 小
    style2 = StyleManager().resolve("sad", "calm")  # speed < 1
    tts.synthesize("ゆっくり", style=style2)
    assert fake.last_params["length"] > 1.0


def test_volume_gain_applied_to_wav():
    tts, fake = _make_tts()
    quiet = StyleManager().resolve(None, "soft")  # volume < 1
    wav_soft, sr = tts.synthesize("ちいさく", style=quiet)
    loud = StyleManager().resolve("joy", "lively")  # volume > 1
    wav_loud, _ = tts.synthesize("おおきく", style=loud)
    assert float(np.abs(wav_soft).max()) < float(np.abs(wav_loud).max())
    assert sr == 44100


def test_style_weight_clamped():
    tts, fake = _make_tts(style_weight=2.5)
    style = StyleManager().resolve("joy", "playful")  # intonation > 1 → weight増
    tts.synthesize("わーい", style=style)
    assert 0.0 <= fake.last_params["style_weight"] <= 3.0


def test_single_style_model_keeps_neutral_weight_stable():
    tts, fake = _make_tts(style_weight=1.0)
    fake._info["0"]["style2id"] = {"Neutral": 0}
    tts._load_model_info()
    playful = StyleManager().resolve("joy", "playful")
    assert playful.intonation > 1.0

    tts.synthesize("発音を安定させる", style=playful)

    assert fake.last_params["style"] == "Neutral"
    assert fake.last_params["style_weight"] == 1.0


def test_single_style_weight_lock_can_be_disabled():
    tts, fake = _make_tts(style_weight=1.0, lock_single_style_weight=False)
    fake._info["0"]["style2id"] = {"Neutral": 0}
    tts._load_model_info()
    playful = StyleManager().resolve("joy", "playful")

    tts.synthesize("従来の強度変化", style=playful)

    assert fake.last_params["style_weight"] > 1.0


def test_speaker_list_and_selection_include_model():
    tts, fake = _make_tts()
    fake._info["1"] = {
        "config_path": "model_assets/second/config.json",
        "spk2id": {"話者A": 0, "話者B": 2},
        "style2id": {"Neutral": 0},
    }
    speakers = tts.list_speakers()
    assert [s["id"] for s in speakers] == [
        "amitaro::0", "second::0", "second::2",
    ]
    assert speakers[-1]["label"] == "second / 話者B"

    tts.set_speaker("second::2")
    assert tts.speaker == "second::2"
    tts.synthesize("切替テスト")
    assert fake.last_params["model_name"] == "second"
    assert fake.last_params["speaker_id"] == 2
