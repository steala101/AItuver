"""Phase 7D: 実経路の計測が、本当に測りたいものを測っているか。

Phase 7C で `TurnLatency` を作ったが、**実経路には既に別の計測の背骨が
あった**（`utils/latency.py` の `TurnMetrics`。`pipeline` と `bot` が
引数で運んでいる）。新しい計測系をもう1つ足すのではなく、そちらを
広げるのが正しい——監査は `docs/audits/2026-08-03_turn_path.md`。

その監査で、既存の背骨に**終点の誤り**が見つかった。

```
音声が出来た時点  ≠  実際に鳴り始めた時点
```

`play_start` という1つの名前に、意味の違う2つが入っていた。
そのせいで再生開始の区間が 0ms として集計され、**合計も実際より
短く出ていた**。ここはその再発を止める。
"""
from __future__ import annotations

import inspect
from pathlib import Path

from neuro_voice.utils.latency import (
    ALL_PAIRS, NOT_APPLICABLE, LatencyWindow, SourceType, TurnMetrics,
)

ROOT = Path(__file__).resolve().parents[1]


def source_of(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ===========================================================================
# 1. 終点は「鳴り始めた時点」
# ===========================================================================


def test_direct_response_uses_the_shared_playback_coordinator():
    """Direct replies must not bypass tracked TTS/playback delivery."""
    text = source_of("neuro_voice/pipeline.py")
    start = text.index("async def _respond_direct_text")
    # 窓を固定長で切ると、関数へ数行足しただけで「見つからない」に
    # なる。**順序を見たいのであって行数を見たいのではない**ので、
    # 関数の終わりまでを窓にする。
    body = text[start:text.index("\n    async def _build_messages", start)]

    assert "await self._speak_loop(" in body
    assert "self._playback.play(" not in body
    assert "_await_delivery_finalization(metrics)" in body


def test_the_streaming_path_marks_playback_from_the_callback():
    """ストリーミング経路は元から正しい。**壊さない。**"""
    text = source_of("neuro_voice/pipeline.py")
    start = text.index("def _on_play_start(")
    body = text[start:text.index("def _on_complete(", start)]
    assert 'mark("play_start")' in body
    # 生成完了は callback の外。
    before = text[:start]
    assert before.rindex('mark("tts_audio_ready")') < start


def test_discord_does_not_treat_the_queue_as_playback():
    """**`source.write()` は Opus のキューへ積むだけ**（テスト2 の Discord 側）。

    ここを `play_start` にしていたので、送出待ちの時間が合計から
    丸ごと抜けていた。
    """
    text = source_of("neuro_voice/discord_bridge/bot.py")
    # The DAVE music mixer may enqueue a voice chunk earlier in the method.
    # This assertion is specifically about the ordinary discord.py queue path.
    start = text.rindex("        self._source.write(pcm)")
    # Delivery diagnostics may add metadata between enqueue and the existing
    # playback start boundary; inspect the full local method, not a fixed
    # character window.
    body = text[start:text.index("    async def _track_discord_chunk", start)]
    enqueued = body.index('mark("discord_enqueued")')
    play_call = body.index("vc.play(self._source)")
    started = body.index('mark("play_start")')
    assert enqueued < play_call < started, "積んだ時点を再生開始にしている"


def test_dave_music_and_tts_share_one_mixed_pcm_writer():
    """DAVE音楽はTTS中も止めず、同じMixerが減衰して送る。"""
    text = source_of("neuro_voice/discord_bridge/bot.py")
    pump_start = text.index("    async def _direct_music_pump")
    pump = text[pump_start:text.index("    async def _omakase_query", pump_start)]
    assert "self._assistant_audio_active()" not in pump
    assert "self._source.read()" in pump
    speak_start = text.index("    async def _speak(")
    speak = text[speak_start:text.index("    async def _track_discord_chunk", speak_start)]
    assert "self._source.write(pcm)" in speak
    assert "self._ensure_direct_music_pump()" in speak


def test_no_path_marks_playback_before_handing_audio_over():
    """**全ての `play_start` が、音を渡した後にあること。**

    1箇所直しても、別の経路が同じことをしていたら意味がない。
    """
    for relative in ("neuro_voice/pipeline.py",
                     "neuro_voice/discord_bridge/bot.py"):
        text = source_of(relative)
        for index in range(len(text)):
            index = text.find('mark("play_start")', index)
            if index < 0:
                break
            window = text[max(0, index - 700):index]
            handed = any(marker in window for marker in (
                "self._playback.play(", "vc.play(", "play_pcm(",
                "def _on_play_start("))
            assert handed, f"{relative} の play_start が音を渡す前にある"
            index += 1


# ===========================================================================
# 2. 通らなかった段階
# ===========================================================================


def test_a_skipped_stage_is_not_zero():
    """**テスト3。** 記憶検索をしなかったターンを 0ms にしない。"""
    metrics = TurnMetrics()
    metrics.mark("speech_end")
    metrics.mark("stt_done")
    metrics.skip("memory_done")
    assert metrics.value("stt_done", "memory_done") == NOT_APPLICABLE
    assert metrics.delta_ms("stt_done", "memory_done") is None


def test_a_broken_stage_is_distinguishable_from_a_skipped_one():
    """**壊れて mark が打たれなかった**のと、やらなかったのを分ける。"""
    broken = TurnMetrics()
    broken.mark("stt_done")
    assert broken.value("stt_done", "memory_done") is None

    skipped = TurnMetrics()
    skipped.mark("stt_done")
    skipped.skip("memory_done")
    assert skipped.value("stt_done", "memory_done") == NOT_APPLICABLE


def test_the_window_counts_skipped_stages_separately():
    window = LatencyWindow(max_samples=10)
    for _ in range(5):
        metrics = TurnMetrics()
        metrics.mark("stt_done")
        metrics.skip("memory_done")
        window.add(metrics)
    entry = next(item for item in window.snapshot() if item["label"] == "記憶検索")
    assert entry["samples"] == 0
    assert entry["not_applicable"] == 5


# ===========================================================================
# 3. 集計
# ===========================================================================


def test_the_window_reports_p50_p90_p95_min_max():
    window = LatencyWindow(max_samples=50)
    for total_ms in range(100, 3100, 100):          # 30 サンプル
        metrics = TurnMetrics({"speech_end": 0.0, "play_start": total_ms / 1000})
        window.add(metrics)
    total = next(item for item in window.snapshot() if item["label"] == "合計")
    for key in ("p50_ms", "p90_ms", "p95_ms", "min_ms", "max_ms", "samples"):
        assert key in total, key
    assert total["samples"] == 30
    assert total["min_ms"] == 100
    assert total["max_ms"] == 3000
    assert total["p50_ms"] < total["p90_ms"] < total["p95_ms"]


def test_percentiles_are_observed_values_not_interpolations():
    """**一度も起きていない値を p95 として出さない。**

    30ターン程度の標本で補間すると、実在しない数字が「95%のターンは
    これより速かった」の根拠になる。
    """
    window = LatencyWindow(max_samples=10)
    for total_ms in (100, 200, 300, 400, 500):
        window.add(TurnMetrics({"speech_end": 0.0, "play_start": total_ms / 1000}))
    total = next(item for item in window.snapshot() if item["label"] == "合計")
    for key in ("p50_ms", "p90_ms", "p95_ms"):
        assert total[key] in {100, 200, 300, 400, 500}, (key, total[key])


def test_local_and_discord_totals_are_not_mixed():
    """**別の集計系は作らないが、合計は経路ごとに分ける。**"""
    window = LatencyWindow(max_samples=50)
    for _ in range(10):
        local = TurnMetrics({"speech_end": 0.0, "play_start": 3.0})
        local.source_type = SourceType.LOCAL_MIC
        window.add(local)
        discord = TurnMetrics({"speech_end": 0.0, "play_start": 9.0})
        discord.source_type = SourceType.DISCORD_VOICE
        window.add(discord)
    totals = {item["source_type"]: item for item in window.totals()}
    assert totals[SourceType.LOCAL_MIC]["p50_ms"] == 3000
    assert totals[SourceType.DISCORD_VOICE]["p50_ms"] == 9000


def test_discord_stages_exist_in_the_shared_structure():
    """Discord 固有の区間も**同じ構造**に入っていること。"""
    labels = {label for _, _, label in ALL_PAIRS}
    assert {"送出キュー", "Discord再生"} <= labels
    assert {"TTS生成", "再生待ち", "合計"} <= labels


# ===========================================================================
# 4. ターンを辿れる
# ===========================================================================


def test_the_metrics_carry_a_turn_id():
    """**途中で作り直さない。** 1ターン1つ。"""
    metrics = TurnMetrics()
    assert metrics.turn_id
    assert TurnMetrics().turn_id != metrics.turn_id


def test_the_snapshot_holds_no_conversation_text():
    metrics = TurnMetrics()
    metrics.session_id = "s1"
    metrics.mark("speech_end")
    metrics.mark("play_start")
    import json

    text = json.dumps(metrics.snapshot(), ensure_ascii=False)
    assert "content" not in text
    assert set(metrics.snapshot()) == {
        "turn_id", "source_type", "session_id", "conversation_id",
        "channel_id", "persona", "origin_turn_id", "stages", "total_ms",
        "skipped", "duplicate_stage"}


def test_the_persona_can_be_bound_to_the_turn():
    from neuro_voice.cognition.persona_scope import PersonaContext

    metrics = TurnMetrics()
    metrics.bind_persona(PersonaContext("poppo", "2", 3))
    assert metrics.persona_id == "poppo"
    assert metrics.persona_version == "2"
    assert metrics.persona_epoch == 3


def test_the_clock_is_monotonic():
    """**壁時計だけで区間を測らない。** NTP 補正で負になる。"""
    source = inspect.getsource(TurnMetrics.mark)
    assert "perf_counter" in source
    assert "time.time" not in source


def test_the_total_ends_at_playback_not_at_audio_ready():
    metrics = TurnMetrics()
    metrics.marks.update({"speech_end": 0.0, "tts_audio_ready": 2.0,
                          "play_start": 3.5})
    assert metrics.total_ms == 3500.0
    assert metrics.delta_ms("tts_audio_ready", "play_start") == 1500.0
