"""Asking the server where the fixed ~1100ms goes.

    初トークン ≈ 1106ms + 0.244ms × (キャッシュされなかったトークン数)

The slope is prompt evaluation.  The intercept is not: 464 uncached tokens —
about 113ms of evaluation — still took 1138ms, with no concurrent request of
ours.  The OpenAI-compatible endpoint discards `load_duration` and friends, so
the probe asks Ollama's native endpoint with a prompt too small to matter, and
does it with and without the per-request `options` we normally send.
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.llm.ollama_probe import ProbeCall, ProbeResult, probe_ollama

MS = 1_000_000


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """Enough of requests.Session to drive the probe."""

    def __init__(self, chat_payload, *, ps_models=(), fail_ps=False):
        self._chat = chat_payload
        self._ps = list(ps_models)
        self._fail_ps = fail_ps
        self.posts: list[dict] = []

    def get(self, url, timeout=None):
        if self._fail_ps:
            raise OSError("no server")
        return FakeResponse({"models": self._ps})

    def post(self, url, json=None, timeout=None):
        self.posts.append(json or {})
        payload = self._chat(json) if callable(self._chat) else self._chat
        return FakeResponse(payload)

    def close(self):
        pass


def install(monkeypatch, session):
    """Stand in for the `requests` module the probe imports at call time."""
    import sys
    import types

    fake = types.ModuleType("requests")
    fake.Session = lambda: session
    monkeypatch.setitem(sys.modules, "requests", fake)


def body(*, load_ms=0, prompt_ms=20, eval_ms=60, tokens=5):
    return {
        "load_duration": load_ms * MS,
        "prompt_eval_duration": prompt_ms * MS,
        "eval_duration": eval_ms * MS,
        "total_duration": (load_ms + prompt_ms + eval_ms) * MS,
        "prompt_eval_count": tokens,
        "message": {"content": "はい"},
    }


# ---------------------------------------------------------------------------
# Reading the server's own numbers
# ---------------------------------------------------------------------------


def test_nanoseconds_become_milliseconds(monkeypatch):
    install(monkeypatch, FakeSession(body(load_ms=900, prompt_ms=15, eval_ms=40)))
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    call = result.calls[0]
    assert call.load_ms == 900.0
    assert call.prompt_eval_ms == 15.0
    assert call.eval_ms == 40.0
    assert call.prompt_tokens == 5


def test_the_v1_suffix_is_stripped_for_the_native_endpoint(monkeypatch):
    session = FakeSession(body())
    install(monkeypatch, session)
    probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert session.posts, "ネイティブ /api/chat を叩く"


def test_the_context_size_is_swept(monkeypatch):
    """The sweep is what tells KV cache allocation from runner setup."""
    session = FakeSession(body())
    install(monkeypatch, session)
    result = probe_ollama("http://localhost:11434/v1", "m", num_ctx=8192, repeats=1)
    sizes = sorted({call.num_ctx for call in result.calls if call.num_ctx})
    assert sizes == [1024, 4096, 8192]


def test_a_control_without_options_is_included(monkeypatch):
    session = FakeSession(body())
    install(monkeypatch, session)
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert any(not call.with_options for call in result.calls)
    assert any("num_ctx" not in (post.get("options") or {}) for post in session.posts)


def test_keep_alive_is_asked_for_once(monkeypatch):
    """The observed expiry was Ollama's 5m default while config said 10m."""
    session = FakeSession(body())
    install(monkeypatch, session)
    probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert any(post.get("keep_alive") == "30m" for post in session.posts)


def test_the_cold_first_request_is_not_recorded(monkeypatch):
    calls = []

    def payload(request):
        calls.append(request)
        return body(load_ms=4000 if len(calls) == 1 else 50)

    install(monkeypatch, FakeSession(payload))
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert all(call.load_ms < 1000 for call in result.calls if not call.error)


def test_the_prompt_is_tiny_so_evaluation_cannot_explain_anything(monkeypatch):
    session = FakeSession(body())
    install(monkeypatch, session)
    probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    content = session.posts[0]["messages"][0]["content"]
    assert len(content) <= 10


def test_unexplained_time_is_what_the_server_did_not_account_for():
    call = ProbeCall(
        with_options=False, round_trip_ms=1000, load_ms=100,
        prompt_eval_ms=50, eval_ms=50,
    )
    assert call.unexplained_ms == 800.0


def test_unexplained_time_never_goes_negative():
    call = ProbeCall(with_options=False, round_trip_ms=10, load_ms=100)
    assert call.unexplained_ms == 0.0


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def verdict_for(calls):
    return ProbeResult(calls=calls).verdict()


def test_a_slow_load_is_named():
    calls = [
        ProbeCall(with_options=True, round_trip_ms=1200, load_ms=1000)
        for _ in range(3)
    ]
    assert "モデル準備" in verdict_for(calls)


def test_unexplained_time_is_named_when_nothing_else_fits():
    calls = [
        ProbeCall(with_options=True, round_trip_ms=1200, load_ms=10,
                  prompt_eval_ms=20, eval_ms=40)
        for _ in range(3)
    ]
    assert "説明していない" in verdict_for(calls)


def test_a_fast_server_says_the_cost_is_elsewhere():
    calls = [
        ProbeCall(with_options=True, round_trip_ms=90, load_ms=0,
                  prompt_eval_ms=20, eval_ms=40)
        for _ in range(3)
    ]
    assert "会話リクエスト固有" in verdict_for(calls)


def test_a_load_time_that_tracks_the_context_size_names_the_kv_cache():
    calls = [
        ProbeCall(with_options=True, num_ctx=1024, round_trip_ms=300, load_ms=120),
        ProbeCall(with_options=True, num_ctx=4096, round_trip_ms=600, load_ms=350),
        ProbeCall(with_options=True, num_ctx=8192, round_trip_ms=900, load_ms=640),
    ]
    verdict = verdict_for(calls)
    assert "KVキャッシュ" in verdict
    assert "比例" in verdict


def test_a_flat_load_time_rules_out_the_kv_cache():
    calls = [
        ProbeCall(with_options=True, num_ctx=1024, round_trip_ms=900, load_ms=602),
        ProbeCall(with_options=True, num_ctx=4096, round_trip_ms=900, load_ms=621),
        ProbeCall(with_options=True, num_ctx=8192, round_trip_ms=900, load_ms=633),
    ]
    verdict = verdict_for(calls)
    assert "KVキャッシュでもモデルの再ロードでもない" in verdict
    assert "31ms" in verdict


def test_the_reload_spike_is_excluded_from_the_steady_state():
    """Changing num_ctx reloads the model; the sweep causes that itself."""
    result = ProbeResult(calls=[
        ProbeCall(with_options=True, num_ctx=1024, round_trip_ms=7003, load_ms=6646),
        ProbeCall(with_options=True, num_ctx=1024, round_trip_ms=834, load_ms=602),
        ProbeCall(with_options=True, num_ctx=8192, round_trip_ms=7206, load_ms=6901),
        ProbeCall(with_options=True, num_ctx=8192, round_trip_ms=867, load_ms=633),
    ])
    assert result._load_by_context() == [(1024, 602.0), (8192, 633.0)]
    # 設定値(=最大)での定常値。本番が走るのはこの条件
    assert result.steady_load_ms == 633.0
    assert result.reload_ms == 6646.0


def test_the_reload_cost_is_reported_separately():
    result = ProbeResult(calls=[
        ProbeCall(with_options=True, num_ctx=1024, round_trip_ms=7003, load_ms=6646),
        ProbeCall(with_options=True, num_ctx=1024, round_trip_ms=834, load_ms=602),
        ProbeCall(with_options=True, num_ctx=8192, round_trip_ms=867, load_ms=633),
    ])
    summary = result.summary()
    assert "再ロード" in summary
    assert "定常" in summary


def test_an_unchanged_expiry_means_keep_alive_is_ignored():
    result = ProbeResult(
        loaded_models=["m: 合計1MB / VRAM1MB — 全部GPU / 期限=2026-07-27T23:27:50"],
        loaded_after=["m: 合計1MB / VRAM1MB — 全部GPU / 期限=2026-07-27T23:27:50"],
        calls=[ProbeCall(True, num_ctx=8192, round_trip_ms=100,
                         load_ms=10, prompt_eval_ms=20, eval_ms=40)],
    )
    assert "keep_alive" in result.verdict()


# ---------------------------------------------------------------------------
# Failing safely
# ---------------------------------------------------------------------------


def test_a_dead_server_is_reported_not_raised(monkeypatch):
    class Dead(FakeSession):
        def post(self, url, json=None, timeout=None):
            raise OSError("connection refused")

    install(monkeypatch, Dead(body(), fail_ps=True))
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert result.calls[0].error
    assert "判定不能" in result.verdict()


def test_a_missing_ps_endpoint_does_not_stop_the_probe(monkeypatch):
    install(monkeypatch, FakeSession(body(), fail_ps=True))
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert result.loaded_models == []
    assert result.calls


def test_loaded_models_are_listed(monkeypatch):
    session = FakeSession(body(), ps_models=[
        {"name": "gemma4:12b-it-qat",
         "size": 9 * 1024 ** 3, "size_vram": 9 * 1024 ** 3},
    ])
    install(monkeypatch, session)
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert "gemma4:12b-it-qat" in result.loaded_models[0]
    assert "全部GPU" in result.loaded_models[0]


def test_a_model_that_does_not_fit_in_vram_is_called_out(monkeypatch):
    """The decisive number: resident is not the same as resident on the GPU."""
    session = FakeSession(body(), ps_models=[
        {"name": "gemma4:12b-it-qat",
         "size": 9 * 1024 ** 3, "size_vram": 7 * 1024 ** 3},
    ])
    install(monkeypatch, session)
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert "溢れている" in result.loaded_models[0]
    assert "2048MB" in result.loaded_models[0]


def test_spilled_vram_changes_the_verdict():
    calls = [
        ProbeCall(with_options=True, num_ctx=size, round_trip_ms=900, load_ms=600)
        for size in (1024, 4096, 8192)
    ]
    fits = ProbeResult(
        loaded_models=["m: 合計9000MB / VRAM9000MB — 全部GPU"], calls=calls,
    )
    spills = ProbeResult(
        loaded_models=["m: 合計9000MB / VRAM7000MB — **CPUへ2000MB溢れている** (GPU上は78%)"],
        calls=calls,
    )
    assert "VRAMに収まりきっていない" in spills.verdict()
    assert "VRAMに収まりきっていない" not in fits.verdict()
    # 収まっている場合は num_ctx の掃引で原因を切り分ける
    assert "KVキャッシュでもモデルの再ロードでもない" in fits.verdict()


def test_the_summary_is_one_readable_block(monkeypatch):
    install(monkeypatch, FakeSession(body(load_ms=700)))
    summary = probe_ollama("http://localhost:11434/v1", "m", repeats=2).summary()
    assert "読み込み済みモデル" in summary
    assert "判定" in summary


@pytest.mark.parametrize("value", [None, "", "abc"])
def test_a_missing_duration_is_zero_not_an_error(monkeypatch, value):
    install(monkeypatch, FakeSession({"load_duration": value, "message": {}}))
    result = probe_ollama("http://localhost:11434/v1", "m", repeats=1)
    assert result.calls[0].load_ms == 0.0
