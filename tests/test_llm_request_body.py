"""What actually gets sent alongside the messages.

This file exists because of a break I caused: `keep_alive` was added to the
request using `self._keep_alive`, an attribute that did not exist — the value
was only ever stored as `_multimodal_keep_alive`.  Nothing caught it, because
nothing exercised the request-building code without a live server.  Every
reply failed at runtime with

    応答生成エラー: 'OpenAICompatBackend' object has no attribute '_keep_alive'

The fix was to pull the dict out of the streaming coroutine into a plain
method, which is the part these tests hold down.
"""
from __future__ import annotations

import pytest

from neuro_voice.llm.openai_compat import OpenAICompatBackend


def backend(**overrides):
    settings = dict(
        name="ollama",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model="gemma4:12b-it-qat",
        num_ctx=8192,
        max_tokens=1024,
        keep_alive="10m",
    )
    settings.update(overrides)
    return OpenAICompatBackend(**settings)


# ---------------------------------------------------------------------------
# The break itself
# ---------------------------------------------------------------------------


def test_building_the_request_body_does_not_raise():
    """The regression: this used to raise AttributeError on every turn."""
    assert isinstance(backend().request_extra_body(), dict)


def test_every_attribute_it_reads_exists():
    instance = backend()
    for name in ("_keep_alive", "_reasoning_effort", "_is_ollama",
                 "_send_options", "_num_ctx", "_max_tokens"):
        assert hasattr(instance, name), name


# ---------------------------------------------------------------------------
# keep_alive
# ---------------------------------------------------------------------------


def test_keep_alive_is_sent():
    """Measured: the configured value never reached the server, so Ollama
    unloaded the model after its own 5 minutes and the next sentence paid a
    full reload."""
    assert backend().request_extra_body()["keep_alive"] == "10m"


def test_an_unset_keep_alive_is_not_sent():
    assert "keep_alive" not in backend(keep_alive=None).request_extra_body()
    assert "keep_alive" not in backend(keep_alive="   ").request_extra_body()


def test_keep_alive_is_only_for_ollama():
    body = backend(name="openai", keep_alive="10m").request_extra_body()
    assert "keep_alive" not in body


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


def test_the_context_size_is_sent_by_default():
    options = backend().request_extra_body()["options"]
    assert options == {"num_ctx": 8192, "num_predict": 1024}


def test_options_can_be_switched_off():
    body = backend(send_ollama_options=False).request_extra_body()
    assert "options" not in body
    assert body.get("keep_alive") == "10m", "keep_alive まで消してはいけない"


def test_options_are_only_for_ollama():
    assert "options" not in backend(name="cerebras").request_extra_body()


# ---------------------------------------------------------------------------
# reasoning_effort
# ---------------------------------------------------------------------------


def test_reasoning_effort_is_passed_raw():
    """The SDK validates its enum client-side and "none" is outside it."""
    assert backend(reasoning_effort="none").request_extra_body()[
        "reasoning_effort"
    ] == "none"


def test_ollama_defaults_to_no_reasoning():
    assert backend().request_extra_body()["reasoning_effort"] == "none"


def test_a_hosted_backend_without_a_setting_sends_nothing():
    assert backend(name="openai", keep_alive=None).request_extra_body() == {}


@pytest.mark.parametrize("name", ["ollama", "openai", "cerebras", "openrouter"])
def test_the_body_is_json_serialisable_for_every_backend(name):
    import json

    json.dumps(backend(name=name).request_extra_body())


# ---------------------------------------------------------------------------
# サンプリングのつまみ
#
# 「同じ入力に同じ返答」に直接効くのは温度だけで、他は未設定なら送らない。
# 中途半端な値を勝手に送ると、モデル自身が想定している組み合わせを崩す。
# ---------------------------------------------------------------------------


def test_sampling_knobs_are_not_sent_unless_configured():
    """未設定の項目を既定値で埋めない。モデルの推奨値が使われる。"""
    options = backend().request_extra_body()["options"]
    assert set(options) == {"num_ctx", "num_predict"}


@pytest.mark.parametrize("key,value", [
    ("top_p", 0.95), ("top_k", 64), ("min_p", 0.05),
])
def test_a_configured_knob_is_sent(key, value):
    options = backend(**{key: value}).request_extra_body()["options"]
    assert options[key] == value


def test_only_the_configured_knobs_are_sent():
    options = backend(top_k=64).request_extra_body()["options"]
    assert options["top_k"] == 64
    assert "top_p" not in options and "min_p" not in options


def test_the_knobs_are_ollama_only():
    """OpenAI互換の他社エンドポイントへ Ollama 固有の options を送らない。"""
    other = backend(name="openai", top_k=64)
    assert "options" not in other.request_extra_body()
