"""What a failure looks like to the person using the app.

Observed: a TTS backend puts the utterance in the request query string.  When
the request failed, the exception carried the whole URL, and the chat window
filled with the person's own sentence percent-encoded:

    %E3%81%99%E3%81%8E%E3%82%8B%E3%82%88%E3%81%89%EF%BC%81...
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
import requests

from neuro_voice.utils.errors import safe_error_text


def test_a_request_url_never_reaches_the_person():
    error = requests.HTTPError(
        "414 Client Error: Request-URI Too Long for url: "
        "http://127.0.0.1:5000/voice?text=%E3%81%99%E3%81%8E%E3%82%8B%E3%82%88"
        "%E3%81%89%EF%BC%81%E3%81%8D%E3%81%A3%E3%81%A6&speaker_id=0"
    )
    message = safe_error_text(error)
    assert "%E3%81" not in message
    assert "http://" not in message
    assert "414" in message


def test_a_percent_encoded_run_is_collapsed_even_without_a_url():
    message = safe_error_text("合成失敗 %E3%81%99%E3%81%8E%E3%82%8B%E3%82%88 の処理中")
    assert "%E3%81" not in message
    assert "合成失敗" in message


def test_a_short_percent_sequence_is_left_alone():
    """A stray '%20' is not a leaked payload."""
    assert "%20" in safe_error_text("bad path a%20b")


def test_a_plain_message_survives():
    assert safe_error_text(ValueError("音声デバイスが見つかりません")) == "音声デバイスが見つかりません"


def test_an_empty_exception_falls_back_to_its_type():
    assert safe_error_text(TimeoutError()) == "TimeoutError"


def test_a_url_only_message_falls_back_to_its_type():
    assert safe_error_text(ConnectionError("https://example.com/x")) == "ConnectionError"


def test_long_messages_are_truncated():
    message = safe_error_text(RuntimeError("あ" * 500), limit=80)
    assert len(message) <= 80
    assert message.endswith("…")


def test_newlines_are_flattened():
    assert "\n" not in safe_error_text(RuntimeError("一行目\n二行目\n三行目"))


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_is_handled(value):
    assert safe_error_text(value) == "不明なエラー"


def test_tts_http_failure_emits_and_logs_only_safe_metadata(caplog):
    from neuro_voice.pipeline import VoicePipeline
    from neuro_voice.utils.latency import TurnMetrics

    spoken = "秘密を含み得る読み上げ文" * 20
    leaked_url = (
        "http://127.0.0.1:5000/voice?text="
        "%E7%A7%98%E5%AF%86%E3%82%92%E5%90%AB%E3%82%80&speaker_id=0"
    )
    response = requests.Response()
    response.status_code = 422
    response.url = leaked_url
    error = requests.HTTPError(
        f"422 Client Error: Unprocessable Entity for url: {leaked_url}",
        response=response,
    )

    class Turns:
        def note_text(self, *_args, **_kwargs):
            pass

        def commit_chunk(self, *_args, **_kwargs):
            return SimpleNamespace(key="tts-job")

        def blocks(self, _verdict):
            return False

        def seal_playback_session(self, *_args, **_kwargs):
            pass

    emitted = []
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._cfg = SimpleNamespace(get=lambda _key, default=None: default)
    pipeline._mind = None
    pipeline._turns = Turns()
    pipeline._speech_allowed_now = lambda *_args, **_kwargs: True
    pipeline._without_internal_leak = lambda text: text
    pipeline._emit = lambda kind, **payload: emitted.append((kind, payload))

    async def fail(*_args, **_kwargs):
        raise error

    pipeline._synthesize_with_delivery = fail

    async def scenario():
        queue = asyncio.Queue()
        await queue.put(spoken)
        await queue.put(None)
        await VoicePipeline._speak_loop(
            pipeline, queue, TurnMetrics(turn_id="tts:error"),
            enforce_conversation_contract=False,
        )

    with caplog.at_level(logging.WARNING, logger="neuro_voice.pipeline"):
        asyncio.run(scenario())

    assert emitted == [(
        "error", {"message": "TTS合成エラー: HTTPError (HTTP 422)"},
    )]
    combined = caplog.text + repr(emitted)
    assert "HTTPError" in combined
    assert "422" in combined
    assert spoken not in combined
    assert leaked_url not in combined
    assert "%E7%A7%98" not in combined


def test_discord_tts_http_failure_emits_and_logs_only_safe_metadata(caplog):
    from neuro_voice.discord_bridge.bot import DiscordBridge

    spoken = "Discordでも秘密を含み得る読み上げ文" * 20
    leaked_url = (
        "http://127.0.0.1:5000/voice?text="
        "%E7%A7%98%E5%AF%86%E3%82%92%E5%90%AB%E3%82%80&speaker_id=0"
    )
    response = requests.Response()
    response.status_code = 422
    response.url = leaked_url
    error = requests.HTTPError(
        f"422 Client Error: Unprocessable Entity for url: {leaked_url}",
        response=response,
    )

    emitted = []
    bridge = DiscordBridge.__new__(DiscordBridge)
    bridge._mind = None
    bridge._speech_allowed_now = lambda: True
    bridge._emit = lambda kind, **payload: emitted.append((kind, payload))

    async def fail(*_args, **_kwargs):
        raise error

    bridge._synthesize_with_delivery = fail

    async def scenario():
        await DiscordBridge._speak(
            bridge, asyncio.get_running_loop(), spoken,
            enforce_conversation_contract=False,
        )

    with caplog.at_level(logging.WARNING, logger="neuro_voice.discord_bridge.bot"):
        asyncio.run(scenario())

    assert emitted == [(
        "error", {"message": "Discord TTS合成エラー: HTTPError (HTTP 422)"},
    )]
    combined = caplog.text + repr(emitted)
    assert "HTTPError" in combined
    assert "422" in combined
    assert spoken not in combined
    assert leaked_url not in combined
    assert "%E7%A7%98" not in combined
