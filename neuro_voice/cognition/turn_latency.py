"""ユーザーが話し終えてから、最初の音が出るまでの内訳。

実機で応答開始が 3000ms から 4000〜5000ms へ悪化した。困ったのは
**どこが増えたか分からない**こと——合計しか測っていなかったので、
記憶検索なのか、世界状態なのか、Phase 7B で足した処理なのかを
推測でしか言えない。

ここは2つを分ける:

* **待ち時間と実処理時間**（キューで待っていたのか、重かったのか）
* **会話の種類**（通常会話とツール会話を混ぜて平均を出さない）

平均だけ見ない。**p95 が悪いのに p50 が良い**のは、たまに重い処理が
挟まっているということで、直す場所が全く違う。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.cognition.turn_integrity import ConversationKind

#: 測る工程。**この順に流れる。**
STAGES: tuple[str, ...] = (
    "vad_end_to_asr_final_ms",
    "turn_finalize_ms",
    "state_snapshot_ms",
    "memory_trigger_ms",
    "memory_retrieval_ms",
    "world_state_snapshot_ms",
    "persona_context_build_ms",
    "action_selection_ms",
    "conversation_plan_ms",
    # **待ちと実処理を分ける。** 混ぜると「LLMが遅い」で終わってしまう。
    "llm_queue_wait_ms",
    "llm_first_token_ms",
    "surface_realizer_ms",
    "speech_gate_ms",
    "tts_queue_wait_ms",
    "tts_first_audio_ms",
)
#: 合計。**これがユーザーの体感。**
TOTAL = "turn_end_to_first_audio_ms"

#: 待ち時間の工程。実処理と足し合わせて「重い」と言わない。
QUEUE_STAGES: frozenset[str] = frozenset({
    "llm_queue_wait_ms", "tts_queue_wait_ms",
})


@dataclass(slots=True)
class TurnLatency:
    """1ターン分の内訳。"""

    turn_id: str = ""
    kind: ConversationKind | str = ConversationKind.NORMAL_CONVERSATION
    stages: dict[str, float] = field(default_factory=dict)
    #: 有効だった機能。**どれを入れると何ms増えるかを後から引くため。**
    features: dict[str, bool] = field(default_factory=dict)
    streaming: bool = False
    started_at: float = field(default_factory=time.time)
    _marks: dict[str, float] = field(default_factory=dict)

    def start(self, stage: str) -> None:
        self._marks[str(stage)] = time.perf_counter()

    def stop(self, stage: str) -> float:
        mark = self._marks.pop(str(stage), None)
        if mark is None:
            return .0
        value = round((time.perf_counter() - mark) * 1000, 2)
        self.stages[str(stage)] = value
        return value

    def mark(self, stage: str, milliseconds: float) -> None:
        self.stages[str(stage)] = round(float(milliseconds), 2)

    def note_features(self, **flags: bool) -> None:
        self.features.update({k: bool(v) for k, v in flags.items()})

    @property
    def total(self) -> float:
        if TOTAL in self.stages:
            return self.stages[TOTAL]
        return round(sum(self.stages.get(name, .0) for name in STAGES), 2)

    @property
    def queue_time(self) -> float:
        return round(sum(self.stages.get(name, .0)
                         for name in QUEUE_STAGES), 2)

    @property
    def work_time(self) -> float:
        return round(self.total - self.queue_time, 2)

    def snapshot(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id, "kind": str(self.kind),
            "stages": dict(self.stages), "total_ms": self.total,
            "queue_ms": self.queue_time, "work_ms": self.work_time,
            "features": dict(self.features), "streaming": self.streaming,
        }


def percentile(values: list[float], ratio: float) -> float:
    """**平均を出さない。** 遅いターンは平均に埋もれる。"""
    if not values:
        return .0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = ratio * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 2)


@dataclass(frozen=True, slots=True)
class StageSummary:
    stage: str
    count: int
    p50: float
    p90: float
    p95: float
    worst: float

    def snapshot(self) -> dict[str, Any]:
        return {"stage": self.stage, "count": self.count, "p50": self.p50,
                "p90": self.p90, "p95": self.p95, "worst": self.worst}


class LatencyRecorder:
    """ターンをためて、工程別に percentile を出す。

    **会話の種類ごとに分ける。** ツール会話は元々長いので、混ぜると
    通常会話の悪化が見えなくなる。
    """

    def __init__(self, *, capacity: int = 200) -> None:
        self._capacity = int(capacity)
        self._turns: list[TurnLatency] = []
        self._lock = threading.RLock()

    def record(self, turn: TurnLatency) -> None:
        with self._lock:
            self._turns.append(turn)
            del self._turns[:-self._capacity]

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    def turns(self, kind: Any = None) -> list[TurnLatency]:
        with self._lock:
            if kind is None:
                return list(self._turns)
            return [item for item in self._turns if str(item.kind) == str(kind)]

    def summary(self, *, kind: Any = None,
                warmup: int = 0) -> dict[str, StageSummary]:
        """工程ごとの p50 / p90 / p95。

        `warmup` は最初の数ターンを捨てる数。**モデルの初回読み込みを
        含めた数字で「遅い」と言わない。**
        """
        turns = self.turns(kind)[int(warmup):]
        out: dict[str, StageSummary] = {}
        for stage in (*STAGES, TOTAL):
            values = [item.stages[stage] for item in turns
                      if stage in item.stages]
            if not values:
                continue
            out[stage] = StageSummary(
                stage=stage, count=len(values),
                p50=percentile(values, .50), p90=percentile(values, .90),
                p95=percentile(values, .95), worst=round(max(values), 2))
        return out

    def compare(self, feature: str, *, kind: Any = None,
                warmup: int = 0) -> dict[str, Any]:
        """その機能を入れると何ms増えるか。

        **フラグを1つずつ動かして測る**ための入口。同じ入力・同じ
        モデル・同じ端末で比べないと意味がない。
        """
        turns = self.turns(kind)[int(warmup):]
        on = [item.total for item in turns if item.features.get(feature)]
        off = [item.total for item in turns if not item.features.get(feature)]
        if not on or not off:
            return {"feature": feature, "comparable": False,
                    "on_count": len(on), "off_count": len(off)}
        on_p50, off_p50 = percentile(on, .50), percentile(off, .50)
        return {
            "feature": feature, "comparable": True,
            "on_p50": on_p50, "off_p50": off_p50,
            "delta_p50_ms": round(on_p50 - off_p50, 2),
            "on_p95": percentile(on, .95), "off_p95": percentile(off, .95),
            "on_count": len(on), "off_count": len(off),
        }

    def snapshot(self, *, kind: Any = None, warmup: int = 0) -> dict[str, Any]:
        summary = self.summary(kind=kind, warmup=warmup)
        return {
            "kind": str(kind) if kind is not None else "all",
            "turns": len(self.turns(kind)),
            "stages": {name: item.snapshot() for name, item in summary.items()},
        }


# ---------------------------------------------------------------------------
# 通常会話でツール経路へ入らないための、軽い判定
# ---------------------------------------------------------------------------

#: 「これは道具の話だ」と言える言い回し。**ここに当たらなければ、
#: Tool Capability も Plan Admission も Permission も動かさない。**
#:
#: 判定を賢くしようとして LLM を呼ぶと、それ自体が critical path の
#: 遅延になる。**軽い判定で終わらせるのが目的。**
_TOOL_HINTS: tuple[str, ...] = (
    "調べて", "検索して", "ぐぐって", "ググって", "探して",
    "作って", "書いて保存", "ファイルを", "保存して", "メモして",
    "送って", "送信して", "投稿して", "消して", "削除して",
    "search for", "look up", "create a file", "save this",
)
#: 打ち消し。**「調べてたの?」は依頼ではない**（Phase 6 で踏んだ）。
_TOOL_NEGATIONS: tuple[str, ...] = (
    "調べてた", "調べてるの", "調べてたの", "調べなくていい", "調べないで",
    "検索しないで", "作らなくていい", "送らないで", "保存しないで",
)


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """道具の出番か。**既定は「違う」。**"""

    wanted: bool = False
    reason: str = "no_tool_hint"
    matched: str = ""
    decided_ms: float = .0

    @property
    def skip_tool_path(self) -> bool:
        return not self.wanted


def detect_tool_intent(text: str) -> ToolIntent:
    """**通常会話をツール経路へ入れない**ための最初の関門。

    Phase 7B までは、ツールのフラグが off でも Plan Admission と
    Permission の判定までは走っていた。1ターンあたりは小さいが、
    通常会話の critical path に載っている以上、**払う理由が無い**。
    """
    started = time.perf_counter()

    def done(wanted: bool, reason: str, matched: str = "") -> ToolIntent:
        return ToolIntent(wanted, reason, matched,
                          round((time.perf_counter() - started) * 1000, 4))

    value = str(text or "")
    if not value.strip():
        return done(False, "empty")
    lowered = value.lower()
    for marker in _TOOL_NEGATIONS:
        if marker in value or marker in lowered:
            return done(False, "negated", marker)
    for marker in _TOOL_HINTS:
        if marker in value or marker in lowered:
            return done(True, "explicit_request", marker)
    return done(False, "no_tool_hint")


__all__ = [
    "QUEUE_STAGES", "STAGES", "TOTAL", "LatencyRecorder", "StageSummary",
    "ToolIntent", "TurnLatency", "detect_tool_intent", "percentile",
]
