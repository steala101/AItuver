"""Phase 7D ②③: `TurnFrame` と `TurnCommitLedger` が実経路に挿さっているか。

Phase 7C で型もテストも揃っていたのに、**呼び出し側が無かった**。
`tests/test_turn_integrity.py` は型を通しても、それが呼ばれていない
ことは見つけられない——だからここは**配線そのもの**を見る。

見たいのは4つ:

* 1ターンで枠を**作り直していない**（`turn_id` が正本）
* 重複を検出したら、**どの層か**が `TurnMetrics` に残る
* 既定では**止めない**（記録だけ）
* 中断した再生は**確定を取り消す**（重複を嫌って欠落を作らない）
"""
from __future__ import annotations

import re
from pathlib import Path

from neuro_voice.cognition.turn_integrity import (
    CommitKind, DuplicateSite, TextStage,
)
from neuro_voice.cognition.turn_tracker import (
    DISABLED, PASSTHROUGH, TurnTracker, tracker_from_config,
)
from neuro_voice.utils.latency import SourceType, TurnMetrics

ROOT = Path(__file__).resolve().parents[1]


def source_of(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class FakeConfig:
    def __init__(self, values: dict) -> None:
        self._values = dict(values)

    def get(self, name, fallback=None):
        return self._values.get(name, fallback)


# ===========================================================================
# 1. ターンの枠は1つ
# ===========================================================================


def test_the_frame_is_created_once_per_turn():
    """**途中で作り直さない。** 作り直すとどこで増えたか追えなくなる。"""
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    first = tracker.begin(metrics)
    second = tracker.begin(metrics)
    assert first is not None
    assert first is second
    assert first.turn_id == metrics.turn_id


def test_two_turns_get_two_frames():
    tracker = TurnTracker(enabled=True)
    one = tracker.begin(TurnMetrics())
    two = tracker.begin(TurnMetrics())
    assert one is not None and two is not None
    assert one.turn_id != two.turn_id


def test_old_frames_are_dropped():
    """**無限に貯めない。** 診断のための記録で記憶を食い潰さない。"""
    tracker = TurnTracker(enabled=True, capacity=2)
    kept = [tracker.begin(TurnMetrics()) for _ in range(4)]
    assert tracker.snapshot()["frames"] == 2
    assert tracker.frame(kept[0].turn_id) is None
    assert tracker.frame(kept[-1].turn_id) is kept[-1]


def test_the_frame_carries_the_persona_of_the_turn():
    """ペルソナは**ターン開始時のもの**。後から読み直さない。"""
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics(persona_id="poppo", persona_version="2",
                          persona_epoch=3)
    frame = tracker.begin(metrics)
    assert frame.active_persona_id == "poppo"
    assert frame.active_persona_version == "2"
    assert frame.persona_epoch == 3


# ===========================================================================
# 2. 重複した層が残る
# ===========================================================================


def test_a_second_playback_records_the_layer():
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    assert tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1").accepted
    assert not tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1").accepted
    assert metrics.duplicate_stage == str(DuplicateSite.PLAYBACK_DUPLICATE)
    assert metrics.duplicate_detail["key"] == "chunk-1"


def test_delivery_snapshot_counts_speech_tts_and_playback_without_text():
    """重複時に、どの送出層が増えたかを本文なしで追える。"""
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    tracker.commit(metrics, CommitKind.SPEECH_REQUEST, "reply-1")
    tracker.commit_chunk(metrics, "reply-1", 0)
    tracker.commit(metrics, CommitKind.PLAYBACK, "reply-1#0")
    tracker.commit(metrics, CommitKind.PLAYBACK, "reply-1#0")

    delivery = tracker.delivery_snapshot(metrics)
    assert delivery["speech_request"] == {"attempted": 1, "accepted": 1, "rejected": 0}
    assert delivery["tts_job"] == {"attempted": 1, "accepted": 1, "rejected": 0}
    assert delivery["playback"] == {"attempted": 2, "accepted": 1, "rejected": 1}
    assert delivery["first_duplicate_stage"] == str(DuplicateSite.PLAYBACK_DUPLICATE)


def test_streaming_chunks_are_one_logical_playback_session():
    """複数 chunk は正常なストリーミングで、二重再生ではない。"""
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    tracker.commit(metrics, CommitKind.SPEECH_REQUEST, "request-1")
    for index in range(2):
        job_id = f"request-1#{index}"
        assert tracker.commit_chunk(metrics, "request-1", index).accepted
        tracker.note_tts_chunk(
            metrics, speech_request_id="request-1", tts_job_id=job_id,
            chunk_index=index, text_hash=f"text-{index}", audio_hash=f"audio-{index}",
        )
        assert tracker.start_playback_segment(
            metrics, speech_request_id="request-1", playback_session_id="session-1",
            playback_id=f"segment-{index}", segment_index=index, tts_job_id=job_id,
            text_hash=f"text-{index}", audio_hash=f"audio-{index}",
        ).accepted
        tracker.mark_playback_segment_started(metrics, f"segment-{index}")
        tracker.complete_playback_segment(metrics, f"segment-{index}", completed=True)
    tracker.seal_playback_session(metrics, "session-1")

    delivery = tracker.delivery_snapshot(metrics)
    assert delivery["speech_request_count"] == 1
    assert delivery["tts_chunk_count"] == 2
    assert delivery["logical_playback_session_count"] == 1
    assert delivery["playback_segment_count"] == 2
    assert delivery["unique_playback_segment_count"] == 2
    assert delivery["replayed_segment_count"] == 0
    assert not delivery["overlap_detected"]
    assert delivery["first_duplicate_stage"] == "none"


def test_a_replayed_segment_is_rejected_even_in_observation_mode():
    tracker = TurnTracker(enabled=True, suppress_duplicates=False)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    first = tracker.start_playback_segment(
        metrics, speech_request_id="request-1", playback_session_id="session-1",
        playback_id="segment-1", segment_index=0, tts_job_id="job-1",
        text_hash="text", audio_hash="audio",
    )
    second = tracker.start_playback_segment(
        metrics, speech_request_id="request-1", playback_session_id="session-1",
        playback_id="segment-1", segment_index=0, tts_job_id="job-1",
        text_hash="text", audio_hash="audio",
    )
    assert first.accepted and not second.accepted
    delivery = tracker.delivery_snapshot(metrics)
    assert delivery["replayed_segment_count"] == 1
    assert delivery["first_duplicate_stage"] == str(DuplicateSite.PLAYBACK_SEGMENT)


def test_same_segment_index_and_audio_hash_is_rejected_with_a_new_id():
    tracker = TurnTracker(enabled=True, suppress_duplicates=False)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    assert tracker.start_playback_segment(
        metrics, speech_request_id="request-1", playback_session_id="session-1",
        playback_id="segment-original", segment_index=0, tts_job_id="job-1",
        text_hash="text", audio_hash="audio",
    ).accepted
    repeated = tracker.start_playback_segment(
        metrics, speech_request_id="request-1", playback_session_id="session-1",
        playback_id="segment-redelivery", segment_index=0, tts_job_id="job-1",
        text_hash="text", audio_hash="audio",
    )
    assert not repeated.accepted
    assert metrics.duplicate_stage == str(DuplicateSite.PLAYBACK_SEGMENT)


def test_a_second_logical_session_for_one_request_is_rejected():
    tracker = TurnTracker(enabled=True, suppress_duplicates=False)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    assert tracker.start_playback_segment(
        metrics, speech_request_id="request-1", playback_session_id="session-1",
        playback_id="segment-1", segment_index=0, tts_job_id="job-1",
        text_hash="text", audio_hash="audio",
    ).accepted
    rejected = tracker.start_playback_segment(
        metrics, speech_request_id="request-1", playback_session_id="session-2",
        playback_id="segment-2", segment_index=1, tts_job_id="job-2",
        text_hash="text-2", audio_hash="audio-2",
    )
    assert not rejected.accepted
    assert metrics.duplicate_stage == str(DuplicateSite.PLAYBACK_SESSION)


def test_one_utterance_cannot_produce_two_responses():
    """**1つの決定から出せる最終発話は1件。**"""
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    assert tracker.commit(metrics, CommitKind.SPEECH_REQUEST, "resp-a",
                          decision_id="utt-1").accepted
    assert not tracker.commit(metrics, CommitKind.SPEECH_REQUEST, "resp-b",
                              decision_id="utt-1").accepted
    assert metrics.duplicate_stage == str(
        DuplicateSite.SPEECH_REQUEST_DUPLICATE)


def test_a_second_speak_loop_collides_on_the_first_chunk():
    """同じ応答に発話ループが2つ立つと、**両方が 0 番から始まる**。

    断片ごとに新しい ID を振ると、この検査は常に通ってしまい、
    検査しているつもりで何も見ていないことになる。
    """
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    for index in range(3):
        assert tracker.commit_chunk(metrics, "resp-a", index).accepted
    assert not tracker.commit_chunk(metrics, "resp-a", 0).accepted
    assert metrics.duplicate_stage == str(DuplicateSite.TTS_JOB_DUPLICATE)


def test_generation_level_repetition_wins_over_playback():
    """**上流を先に確定させる。** 生成が繰り返しているのに
    「再生の問題」と記録したら、直す場所を探す所が変わってしまう。
    """
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    tracker.note_text(metrics, TextStage.LLM_OUTPUT,
                      "調べてみるね。調べてみるね。")
    assert metrics.duplicate_stage == str(
        DuplicateSite.LLM_GENERATION_DUPLICATE)
    tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1")
    tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1")
    assert metrics.duplicate_stage == str(
        DuplicateSite.LLM_GENERATION_DUPLICATE)


def test_ordinary_text_is_not_reported_as_duplicate():
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    tracker.note_text(metrics, TextStage.LLM_OUTPUT,
                      "調べてみるね。少し待ってて。")
    assert metrics.duplicate_stage == ""


def test_intentional_repetition_is_not_a_commit_failure():
    """「いや、いや」を**台帳が止めることはない**。

    台帳は ID しか見ない。文章の繰り返しを削るかどうかは
    `collapse_adjacent_duplicates` の担当で、ここではない。
    """
    tracker = TurnTracker(enabled=True, suppress_duplicates=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    for index, sentence in enumerate(("いや", "いや", "いや")):
        verdict = tracker.commit_chunk(metrics, "resp-a", index)
        assert verdict.accepted, sentence
        assert not tracker.blocks(verdict)


def test_the_full_text_is_never_kept():
    """**本文は残さない**（第12条）。指紋・文数・文字数だけ。"""
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    frame = tracker.begin(metrics)
    secret = "パスワードは1234だよ"
    tracker.note_text(metrics, TextStage.LLM_OUTPUT, secret)
    dumped = repr(frame.snapshot())
    assert secret not in dumped
    assert "1234" not in dumped


def test_the_stage_is_recorded_once_per_turn():
    """ストリーミングで同じ段階が文の数だけ来ても、記録は1件。"""
    tracker = TurnTracker(enabled=True)
    metrics = TurnMetrics()
    frame = tracker.begin(metrics)
    for sentence in ("いち。", "に。", "さん。"):
        tracker.note_text(metrics, TextStage.SPEECH_GATE_PASSED, sentence)
    assert frame.stage_names.count(str(TextStage.SPEECH_GATE_PASSED)) == 1


# ===========================================================================
# 3. 既定では止めない
# ===========================================================================


def test_recording_does_not_block_by_default():
    """**原因が分かる前に止めない。** 止めると欠落が出て、記録も残らない。"""
    tracker = TurnTracker(enabled=True, suppress_duplicates=False)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1")
    verdict = tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1")
    assert not verdict.accepted
    assert not tracker.blocks(verdict)
    assert metrics.duplicate_stage  # 記録はされている


def test_suppression_blocks_only_when_asked():
    tracker = TurnTracker(enabled=True, suppress_duplicates=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1")
    assert tracker.blocks(tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1"))


def test_a_disabled_tracker_accepts_everything():
    """無効時は**今までと同じ挙動**。何も止めず、何も記録しない。"""
    tracker = TurnTracker(enabled=False, suppress_duplicates=True)
    metrics = TurnMetrics()
    assert tracker.begin(metrics) is None
    first = tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1")
    second = tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1")
    assert first.accepted and second.accepted
    assert second.reason == DISABLED
    assert not tracker.blocks(second)
    assert metrics.duplicate_stage == ""


def test_a_missing_key_is_not_a_duplicate():
    """**ID が無いのは重複ではない。** ここで止めると無関係な発話が消える。"""
    tracker = TurnTracker(enabled=True, suppress_duplicates=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    verdict = tracker.commit(metrics, CommitKind.PLAYBACK, "")
    assert verdict.accepted
    assert verdict.reason == PASSTHROUGH
    assert not tracker.blocks(verdict)
    assert metrics.duplicate_stage == ""
    chunk = tracker.commit_chunk(metrics, "", 0)
    assert chunk.accepted and not tracker.blocks(chunk)


# ===========================================================================
# 4. 中断は取り消す
# ===========================================================================


def test_an_interrupted_playback_can_be_played_again():
    """途中で止まった再生を確定のまま残すと、**二度と鳴らない**。

    重複を嫌うあまり欠落を作らないための戻し口。
    """
    tracker = TurnTracker(enabled=True, suppress_duplicates=True)
    metrics = TurnMetrics()
    tracker.begin(metrics)
    assert tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1").accepted
    tracker.release(CommitKind.PLAYBACK, "chunk-1")
    assert tracker.commit(metrics, CommitKind.PLAYBACK, "chunk-1").accepted


# ===========================================================================
# 5. 設定
# ===========================================================================


def test_the_defaults_are_off():
    """**既定を勝手に本番有効にしない**（チビからの継続指示）。"""
    tracker = tracker_from_config(FakeConfig({}))
    assert not tracker.enabled
    assert not tracker.suppressing


def test_suppression_needs_the_frame_too():
    """枠を作らずに止めるのは筋が通らない（どの層かを記録できない）。"""
    tracker = tracker_from_config(FakeConfig({
        "turn_integrity.duplicate_suppression_enabled": True,
    }))
    assert not tracker.suppressing


def test_the_config_keeps_suppression_off():
    """**記録は有効、抑止は無効。**

    `turn_frame_enabled` は 2026-08-04 に有効化した（記録するだけで
    発話の挙動は変わらない）。`duplicate_suppression_enabled` は
    **層が分かるまで false のまま**——原因が分からないうちに止めると、
    重複の代わりに欠落が出て、しかも今度は記録も残らない。
    """
    text = source_of("config/config.yaml")
    section = text[text.index("\nturn_integrity:"):]
    section = section[:section.index("\n\n\n")] if "\n\n\n" in section else section
    assert re.search(r"^  turn_frame_enabled: true$", section, re.M)
    assert re.search(r"^  duplicate_suppression_enabled: false$", section, re.M)


# ===========================================================================
# 6. 実経路に挿さっているか
# ===========================================================================


def test_both_paths_begin_the_turn_where_speech_ends():
    """**`speech_end` を打つ所で枠を作る。** 後から作ると前半が欠ける。"""
    for relative in ("neuro_voice/pipeline.py",
                     "neuro_voice/discord_bridge/bot.py"):
        text = source_of(relative)
        found = False
        for match in re.finditer(r'metrics\.mark\("speech_end"\)', text):
            window = text[match.end():match.end() + 200]
            if "_begin_turn(metrics)" in window:
                found = True
        assert found, f"{relative} が speech_end の直後で枠を作っていない"


def test_both_paths_commit_the_speech_request():
    for relative in ("neuro_voice/pipeline.py",
                     "neuro_voice/discord_bridge/bot.py"):
        text = source_of(relative)
        assert "CommitKind.SPEECH_REQUEST" in text, relative


def test_both_paths_commit_the_playback():
    for relative in ("neuro_voice/pipeline.py",
                     "neuro_voice/discord_bridge/bot.py"):
        text = source_of(relative)
        assert "start_playback_segment(" in text, relative


def test_both_paths_use_logical_session_and_segment_delivery_tracking():
    for relative in ("neuro_voice/pipeline.py", "neuro_voice/discord_bridge/bot.py"):
        text = source_of(relative)
        assert "start_playback_segment(" in text, relative
        assert "mark_playback_segment_started(" in text, relative
        assert "complete_playback_segment(" in text, relative


def test_latency_and_delivery_trace_are_wired_on_both_voice_paths():
    """集計と重複層の証跡は、実際の Local / Discord 経路から出る。"""
    local = source_of("neuro_voice/pipeline.py")
    assert "self._latency_window = LatencyWindow()" in local
    assert "trace.turn_latency = metrics.snapshot()" in local
    assert "trace.speech_delivery = self._turns.delivery_snapshot(metrics)" in local

    discord = source_of("neuro_voice/discord_bridge/bot.py")
    assert "def _emit_discord_trace(" in discord
    assert "self._emit_discord_trace(metrics)" in discord
    assert "trace.turn_latency = metrics.snapshot()" in discord
    assert "trace.speech_delivery = self._turns.delivery_snapshot(metrics)" in discord


def test_the_streaming_loop_counts_chunks_from_zero():
    """断片番号は**発話ループの呼び出しごとに 0 から**。

    通し番号にすると、2つ目のループが 0 から始まらず衝突しない——
    検査しているつもりで何も見ていない状態に戻る。
    """
    text = source_of("neuro_voice/pipeline.py")
    start = text.index("async def _speak_loop(")
    body = text[start:text.index("\n    # ---------- テキストモード", start)]
    assert "chunk_index = 0" in body
    assert "commit_chunk(metrics, speak_job_id, sentence_index)" in body


def test_discord_turns_are_not_counted_as_local():
    """**Local と Discord を混ぜない。**

    `TurnMetrics.source_type` の既定は `local_mic` なので、Discord 側で
    上書きしないと経路別の合計が静かに混ざる。
    """
    text = source_of("neuro_voice/discord_bridge/bot.py")
    start = text.index("def _begin_turn(")
    body = text[start:text.index("def _report_discord_latency(", start)]
    assert "SourceType.DISCORD_VOICE" in body

    local = source_of("neuro_voice/pipeline.py")
    start = local.index("def _begin_turn(")
    body = local[start:local.index("def _report_latency(", start)]
    assert "SourceType.LOCAL_MIC" in body


def test_both_paths_bind_the_persona_outside_the_flag():
    """persona と session_id は**計測の背骨**であって機能フラグの持ち物
    ではない。🩺 のペルソナ欄が空だったのは呼ばれていなかっただけ。
    """
    for relative, end in (("neuro_voice/pipeline.py", "def _report_latency("),
                          ("neuro_voice/discord_bridge/bot.py",
                           "def _report_discord_latency(")):
        text = source_of(relative)
        start = text.index("def _begin_turn(")
        body = text[start:text.index(end, start)]
        assert "bind_persona" in body, relative
        assert "metrics.session_id" in body, relative
        # フラグ判定より前にあること。
        assert body.index("bind_persona") < body.index("self._turns.begin("), relative


def test_the_source_types_are_the_declared_ones():
    assert SourceType.LOCAL_MIC != SourceType.DISCORD_VOICE
