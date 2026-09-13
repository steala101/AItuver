"""ターン毎のレイテンシ計測ユーティリティ。

**ここが実経路の計測の背骨。** `pipeline` と `bot` の両方が
`TurnMetrics` を引数で運んでいるので、新しい計測系をもう1つ作らず、
ここを広げる（Phase 7D。監査は `docs/audits/2026-08-03_turn_path.md`）。

Phase 7D で直したこと:

* **`play_start` の意味を1つにした。** 音声生成の完了
  (`tts_audio_ready`) と、実際に鳴り始めた時点 (`play_start`) が
  同じ名前で混ざっていて、**再生開始の区間が 0ms として集計され、
  合計も実際より短く出ていた。**
* **通らなかった段階と、測れなかった段階を分けた。** どちらも
  「区間を飛ばす」だと、記憶検索をしなかったターンと、記憶検索が
  壊れて mark が打たれなかったターンの区別がつかない。
* **`turn_id` を持たせた。** 1ターンを最後まで辿れるようにする。
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: 通らなかった段階。**0ms ではない**（第3項）。
NOT_APPLICABLE = "not_applicable"

_PAIRS: list[tuple[str, str, str]] = [
    ("speech_end", "stt_done", "STT"),
    ("stt_done", "context_ready", "Context"),
    ("stt_done", "speaker_ready", "Speaker"),
    ("stt_done", "memory_done", "記憶検索"),
    ("stt_done", "persona_ready", "ペルソナ"),
    ("stt_done", "llm_first", "LLM初回"),
    ("llm_first", "tts_first", "TTS初回"),
    # **音声が出来たのと、鳴り始めたのは別。**
    ("tts_first", "tts_audio_ready", "TTS生成"),
    ("tts_audio_ready", "play_start", "再生待ち"),
    ("speech_end", "play_start", "合計"),
]

#: Discord だけで意味を持つ区間。**別の集計系を作らず、ここへ足す。**
_DISCORD_PAIRS: list[tuple[str, str, str]] = [
    ("discord_receive", "stt_done", "受信→STT"),
    ("dave_decrypt", "opus_decode", "復号→デコード"),
    ("tts_audio_ready", "discord_enqueued", "送出キュー"),
    ("discord_enqueued", "play_start", "Discord再生"),
]

#: 集計へ出す区間。Local だけのターンでは Discord の区間が空になる。
ALL_PAIRS: list[tuple[str, str, str]] = [*_PAIRS, *_DISCORD_PAIRS]


class SourceType:
    LOCAL_MIC = "local_mic"
    DISCORD_VOICE = "discord_voice"


@dataclass
class TurnMetrics:
    """1ターン分のタイムスタンプを記録する。

    **単調増加時計を使う**（`perf_counter`）。壁時計だと、NTP の補正や
    サマータイムで区間が負になったり跳ねたりする。
    """

    marks: dict[str, float] = field(default_factory=dict)
    #: このターンの識別子。**途中で作り直さない**（第2項）。
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source_type: str = SourceType.LOCAL_MIC
    session_id: str = ""
    conversation_id: str = ""
    channel_id: str = ""
    person_id: str = ""
    persona_id: str = ""
    persona_version: str = ""
    persona_epoch: int = 0
    effective_cognition_enabled: bool = False
    cognition_rollout_mode: str = "disabled"
    cognition_requested_rollout_mode: str = "disabled"
    cognition_execution_path: str = "legacy"
    cognition_session_epoch: int = 0
    cognition_activation_source: str = "config"
    cognition_config_fingerprint: str = ""
    transport: str = "LOCAL"
    cognitive_fallback_count: int = 0
    cognitive_fallback_reason: str = ""
    cognitive_side_effect_state: dict[str, bool] = field(default_factory=dict)
    input_event_id: str = ""
    #: 発生元のターン。バックグラウンドの出来事に付ける。
    origin_turn_id: str = ""
    #: **通らなかった**と分かっている段階。測れなかったのとは違う。
    skipped: set[str] = field(default_factory=set)
    #: 重複を検出した段階（`TurnCommitLedger` から入る）。
    duplicate_stage: str = ""
    duplicate_detail: dict[str, Any] = field(default_factory=dict)

    def mark(self, name: str) -> None:
        """初回のみ記録する (2回目以降の呼び出しは無視)。"""
        self.marks.setdefault(name, time.perf_counter())

    def skip(self, *names: str) -> None:
        """その段階を通らなかった。**0ms として集計しない。**"""
        for name in names:
            self.skipped.add(str(name))

    def bind_persona(self, context: Any) -> None:
        if context is None:
            return
        self.persona_id = str(getattr(context, "persona_id", ""))
        self.persona_version = str(getattr(context, "persona_version", ""))
        self.persona_epoch = int(getattr(context, "persona_epoch", 0) or 0)

    def delta_ms(self, start: str, end: str) -> float | None:
        if start in self.marks and end in self.marks:
            return (self.marks[end] - self.marks[start]) * 1000
        return None

    def value(self, start: str, end: str) -> float | str | None:
        """区間の値。**通らなかったなら `not_applicable`。**

        `None`（測れなかった）と区別する。混ぜると、壊れて mark が
        打たれなかったターンが「やらなかったターン」に見える。
        """
        delta = self.delta_ms(start, end)
        if delta is not None:
            return delta
        if end in self.skipped or start in self.skipped:
            return NOT_APPLICABLE
        return None

    @property
    def total_ms(self) -> float | None:
        """発話終了から**実際に鳴り始めるまで**。"""
        return self.delta_ms("speech_end", "play_start")

    def report(self) -> str:
        parts = []
        for start, end, label in ALL_PAIRS:
            delta = self.delta_ms(start, end)
            if delta is not None:
                parts.append(f"{label} {delta:.0f}ms")
        return ("⏱  " + " / ".join(parts)) if parts else ""

    def snapshot(self) -> dict[str, Any]:
        """記録用。**会話本文は入れない**（第12条）。"""
        stages: dict[str, Any] = {}
        for start, end, label in ALL_PAIRS:
            value = self.value(start, end)
            if value is None:
                continue
            stages[label] = (value if isinstance(value, str)
                             else round(value, 1))
        return {
            "turn_id": self.turn_id, "source_type": self.source_type,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "channel_id": self.channel_id,
            "persona": {"id": self.persona_id,
                        "version": self.persona_version,
                        "epoch": self.persona_epoch},
            "origin_turn_id": self.origin_turn_id,
            "stages": stages,
            "total_ms": (round(self.total_ms, 1)
                         if self.total_ms is not None else None),
            "skipped": sorted(self.skipped),
            "duplicate_stage": self.duplicate_stage,
        }


class LatencyWindow:
    """直近ターンの p50 / p90 / p95。

    **平均を出さない。** 遅いターンは平均に埋もれる——p95 だけ悪いのは
    「たまに重い処理が挟まる」ことで、直す場所が全く違う。
    """

    def __init__(self, max_samples: int = 50) -> None:
        self._max = max(5, int(max_samples))
        self._values: dict[str, deque[float]] = {
            label: deque(maxlen=self._max) for _, _, label in ALL_PAIRS
        }
        #: 経路ごとの合計。Local と Discord を混ぜない。
        self._totals: dict[str, deque[float]] = {}
        self._skips: dict[str, int] = {}
        self._turns: int = 0

    def add(self, metrics: TurnMetrics) -> list[dict[str, Any]]:
        self._turns += 1
        for start, end, label in ALL_PAIRS:
            value = metrics.value(start, end)
            if isinstance(value, str):
                self._skips[label] = self._skips.get(label, 0) + 1
                continue
            if value is not None:
                self._values[label].append(value)
        total = metrics.total_ms
        if total is not None:
            bucket = self._totals.setdefault(
                str(metrics.source_type), deque(maxlen=self._max))
            bucket.append(total)
        return self.snapshot()

    @staticmethod
    def _percentile(values: list[float], ratio: float) -> float:
        """**実際に観測した値だけを返す**（nearest-rank）。

        補間すると、30ターン程度の標本では**一度も起きていない値**が
        p95 として出る。「95%のターンはこれより速かった」と言う時に、
        その数字が実在しないのは困る。
        """
        if not values:
            return .0
        import math

        index = max(0, min(len(values) - 1,
                           math.ceil(ratio * len(values)) - 1))
        return values[index]

    def snapshot(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for _, _, label in ALL_PAIRS:
            values = sorted(self._values[label])
            skipped = self._skips.get(label, 0)
            if not values:
                if skipped:
                    # **通らなかったことも出す。** 出さないと、
                    # 「測れていない」のか「やっていない」のか分からない。
                    result.append({"label": label, "samples": 0,
                                   "not_applicable": skipped})
                continue
            result.append({
                "label": label,
                "p50_ms": round(self._percentile(values, .50)),
                "p90_ms": round(self._percentile(values, .90)),
                "p95_ms": round(self._percentile(values, .95)),
                "min_ms": round(values[0]),
                "max_ms": round(values[-1]),
                "samples": len(values),
                "not_applicable": skipped,
            })
        return result

    def totals(self) -> list[dict[str, Any]]:
        """経路別の合計。**Local と Discord を混ぜない。**"""
        out: list[dict[str, Any]] = []
        for source, bucket in sorted(self._totals.items()):
            values = sorted(bucket)
            if not values:
                continue
            out.append({
                "source_type": source, "count": len(values),
                "p50_ms": round(self._percentile(values, .50)),
                "p90_ms": round(self._percentile(values, .90)),
                "p95_ms": round(self._percentile(values, .95)),
                "min_ms": round(values[0]), "max_ms": round(values[-1]),
            })
        return out

    @property
    def turns(self) -> int:
        return self._turns
