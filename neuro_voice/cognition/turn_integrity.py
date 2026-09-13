"""1ターンを最後まで追えるようにして、二重に出さない所。

実機で「たまに同じ内容の文を繰り返す」が出た。文章を見比べても、
**どの層で増えたのか分からない**——生成が二度書いたのか、発話要求が
2件になったのか、音声だけ二度鳴ったのか。層が違えば直す場所も違う。

だからここは2つのことをする:

* **1ターンを同じ `turn_id` で追える**ようにする（全文は残さない）
* **一意性を文章比較ではなく ID で保証する**

文章比較で「同じだから消す」をやると、「いや、いや、それは違う」や
「少しずつ、少しずつ進めよう」まで削る。**意図した繰り返しは残す。**
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# 文章の段階
# ---------------------------------------------------------------------------


class TextStage(StrEnum):
    """文章がどこまで来たか。**増えた場所を挟み撃ちにするため。**"""

    LLM_OUTPUT = "llm_output"
    SURFACE_REALIZED = "surface_realized"
    SEGMENTED = "segmented"
    SPEECH_GATE_PASSED = "speech_gate_passed"
    TTS_SUBMITTED = "tts_submitted"
    PLAYBACK_STARTED = "playback_started"


_STAGE_ORDER: tuple[TextStage, ...] = (
    TextStage.LLM_OUTPUT, TextStage.SURFACE_REALIZED, TextStage.SEGMENTED,
    TextStage.SPEECH_GATE_PASSED, TextStage.TTS_SUBMITTED,
    TextStage.PLAYBACK_STARTED,
)

_SENTENCE_SPLIT = re.compile(r"[。．.!！?？\n]+")


def normalise(value: str) -> str:
    """比較用の形。**意味を変える正規化はしない。**

    NFKC・空白の統合・英字の小文字化・末尾の記号除去だけ。ここから先
    （同義語寄せ、語順の並べ替え）は推測になる。推測で消すと、
    言ったつもりのことが消える。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split()).lower()
    return text.strip(" 。．.!！?？、,")


def sentences(value: str) -> list[str]:
    return [item.strip() for item in _SENTENCE_SPLIT.split(str(value or ""))
            if item.strip()]


def text_hash(value: str) -> str:
    """**全文を残さないための指紋。** 同じか違うかだけ分かればよい。"""
    return hashlib.sha256(normalise(value).encode("utf-8")).hexdigest()[:16]


def _bigrams(value: str) -> set[str]:
    text = normalise(value)
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def similarity(left: str, right: str) -> float:
    """2文の近さ。**判定にだけ使い、削除の根拠にはしない。**"""
    first, second = _bigrams(left), _bigrams(right)
    if not first or not second:
        return 1.0 if normalise(left) == normalise(right) else 0.0
    return len(first & second) / len(first | second)


@dataclass(frozen=True, slots=True)
class StageRecord:
    """1段階分。**本文は入れない**（第12条）。"""

    stage: TextStage | str
    text_hash: str = ""
    sentence_count: int = 0
    char_count: int = 0
    #: 隣り合う文の最大類似度。1.0 に近ければ繰り返している。
    max_adjacent_similarity: float = 0.0
    duplicate_adjacent_pairs: int = 0
    at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage), "hash": self.text_hash,
            "sentences": self.sentence_count, "chars": self.char_count,
            "max_adjacent_similarity": round(self.max_adjacent_similarity, 3),
            "duplicate_pairs": self.duplicate_adjacent_pairs,
        }


#: 「同じ文」と見なす近さ。**下げすぎると別のことを言った文まで消える。**
ADJACENT_DUPLICATE = .92


def measure(stage: TextStage, value: str) -> StageRecord:
    items = sentences(value)
    worst = 0.0
    pairs = 0
    for index in range(1, len(items)):
        score = similarity(items[index - 1], items[index])
        worst = max(worst, score)
        if score >= ADJACENT_DUPLICATE:
            pairs += 1
    return StageRecord(
        stage=stage, text_hash=text_hash(value), sentence_count=len(items),
        char_count=len(str(value or "")), max_adjacent_similarity=worst,
        duplicate_adjacent_pairs=pairs)


# ---------------------------------------------------------------------------
# 増えた場所
# ---------------------------------------------------------------------------


class DuplicateSite(StrEnum):
    """どの層で増えたか。**直す場所が層ごとに違う。**"""

    NONE = "none"
    #: 生成された文章そのものが繰り返している。
    LLM_GENERATION_DUPLICATE = "llm_generation_duplicate"
    #: 文章は1件なのに発話要求が2件。
    SPEECH_REQUEST_DUPLICATE = "speech_request_duplicate"
    #: 発話要求は1件なのに TTS ジョブが2件。
    TTS_JOB_DUPLICATE = "tts_job_duplicate"
    #: 同じ SpeechRequest から別の論理再生セッションが始まった。
    PLAYBACK_SESSION = "playback_session_duplicate"
    #: 同じ再生セグメントを二度受理した。
    PLAYBACK_SEGMENT = "playback_segment_duplicate"
    #: 旧名。再生セグメント重複を指す互換 alias。
    PLAYBACK_DUPLICATE = "playback_segment_duplicate"


# ---------------------------------------------------------------------------
# 1ターン
# ---------------------------------------------------------------------------


class ConversationKind(StrEnum):
    NORMAL_CONVERSATION = "normal_conversation"
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT_REPORT = "tool_result_report"
    GAME_WARNING = "game_warning"
    INITIATIVE = "initiative"


@dataclass(slots=True)
class TurnFrame:
    """1ターンの相関ID。**これが無いと、どこで増えたか追えない。**

    実機で困ったのは「同じ内容が2回聞こえた」という事実だけが手元に
    あって、生成・要求・再生のどこが2件だったのか分からないこと。
    ここが揃っていれば、次に起きた時は数を数えるだけで済む。
    """

    session_id: str = ""
    kind: ConversationKind | str = ConversationKind.NORMAL_CONVERSATION
    # -- 人格 ----------------------------------------------------------
    active_persona_id: str = ""
    active_persona_version: str = "1"
    persona_epoch: int = 0
    effective_cognition_enabled: bool = False
    rollout_mode: str = "disabled"
    requested_rollout_mode: str = "disabled"
    cognition_execution_path: str = "legacy"
    cognition_session_epoch: int = 0
    cognition_activation_source: str = "config"
    cognition_config_fingerprint: str = ""
    transport: str = "LOCAL"
    # -- 経路 ----------------------------------------------------------
    input_event_id: str = ""
    action_decision_id: str = ""
    planner_request_id: str = ""
    llm_request_id: str = ""
    llm_response_id: str = ""
    speech_request_id: str = ""
    tts_job_id: str = ""
    playback_commit_id: str = ""
    memory_ids_used: tuple[int, ...] = ()
    reflection_ids_used: tuple[str, ...] = ()
    history_scope: str = ""
    #: **通常会話でここが true なら、それ自体が不具合。**
    tool_path_entered: bool = False
    tool_intent_reason: str = ""
    #: 本文を持たない送出段階別の件数。重複発話の発生層を特定する。
    delivery_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    #: ID・短い指紋・時刻だけの配送証跡。本文やPCMそのものは絶対に持たない。
    delivery_details: dict[str, Any] = field(default_factory=dict)
    stages: list[StageRecord] = field(default_factory=list)
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)

    def note_text(self, stage: TextStage, value: str) -> StageRecord:
        record = measure(stage, value)
        self.stages.append(record)
        return record

    def stage(self, stage: TextStage) -> StageRecord | None:
        for item in reversed(self.stages):
            if str(item.stage) == str(stage):
                return item
        return None

    def duplicate_site(self, *, speech_requests: int = 0, tts_jobs: int = 0,
                       playbacks: int = 0) -> DuplicateSite:
        """どこで増えたかを1つ返す。**上から順に見る。**

        生成が既に繰り返しているのに再生回数だけ見ると「再生の問題」に
        見えてしまう。上流から確定させる。
        """
        generated = self.stage(TextStage.LLM_OUTPUT)
        if generated is not None and generated.duplicate_adjacent_pairs:
            return DuplicateSite.LLM_GENERATION_DUPLICATE
        if speech_requests > 1:
            return DuplicateSite.SPEECH_REQUEST_DUPLICATE
        if tts_jobs > 1:
            return DuplicateSite.TTS_JOB_DUPLICATE
        if playbacks > 1:
            return DuplicateSite.PLAYBACK_DUPLICATE
        return DuplicateSite.NONE

    def snapshot(self) -> dict[str, Any]:
        """**会話全文もペルソナプロンプトも入れない**（第12条・第17項）。"""
        return {
            "turn_id": self.turn_id, "session_id": self.session_id,
            "kind": str(self.kind),
            "persona": {
                "id": self.active_persona_id,
                "version": self.active_persona_version,
                "epoch": self.persona_epoch,
            },
            "cognition": {
                "enabled": self.effective_cognition_enabled,
                "rollout_mode": self.rollout_mode,
                "requested_rollout_mode": self.requested_rollout_mode,
                "execution_path": self.cognition_execution_path,
                "session_epoch": self.cognition_session_epoch,
                "activation_source": self.cognition_activation_source,
                "config_fingerprint": self.cognition_config_fingerprint,
                "transport": self.transport,
            },
            "ids": {
                "input_event_id": self.input_event_id,
                "action_decision_id": self.action_decision_id,
                "planner_request_id": self.planner_request_id,
                "llm_request_id": self.llm_request_id,
                "llm_response_id": self.llm_response_id,
                "speech_request_id": self.speech_request_id,
                "tts_job_id": self.tts_job_id,
                "playback_commit_id": self.playback_commit_id,
            },
            "memory_ids_used": list(self.memory_ids_used[:8]),
            "reflection_ids_used": list(self.reflection_ids_used[:4]),
            "history_scope": self.history_scope,
            "tool_path_entered": self.tool_path_entered,
            "tool_intent_reason": self.tool_intent_reason,
            "delivery": {
                kind: dict(counts)
                for kind, counts in sorted(self.delivery_counts.items())
            },
            "delivery_details": dict(self.delivery_details),
            "stages": [item.snapshot() for item in self.stages],
        }

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(str(item.stage) for item in self.stages)


def missing_stages(frame: TurnFrame) -> tuple[str, ...]:
    """通っていない段階。**途中で消えたターンを見つけるため。**"""
    seen = set(frame.stage_names)
    return tuple(str(item) for item in _STAGE_ORDER if str(item) not in seen)


# ---------------------------------------------------------------------------
# 一意性の正本
# ---------------------------------------------------------------------------


class CommitKind(StrEnum):
    SPEECH_REQUEST = "speech_request"
    TTS_JOB = "tts_job"
    PLAYBACK = "playback"


@dataclass(frozen=True, slots=True)
class CommitVerdict:
    accepted: bool
    kind: CommitKind | str
    key: str = ""
    reason: str = ""


class TurnCommitLedger:
    """**同じものを二度確定させない。** 文章ではなく ID で見る。

    ```
    1 ActionDecision
    → 0 か 1 の ConversationPlan
    → 0 か 1 の final SpeechRequest
    → 0 か 1 の active TTS job
    → 0 か 1 の completed playback
    ```

    ストリーミングの断片は何個あってもよいが、**同じ断片を二度
    確定してはいけない**。
    """

    def __init__(self, *, capacity: int = 256) -> None:
        self._capacity = int(capacity)
        self._committed: dict[str, str] = {}
        self._order: list[str] = []
        self._by_decision: dict[str, str] = {}
        self._chunks: set[str] = set()
        self._lock = threading.RLock()

    def _remember(self, key: str, value: str) -> None:
        self._committed[key] = value
        self._order.append(key)
        while len(self._order) > self._capacity:
            self._committed.pop(self._order.pop(0), None)

    def claim(self, kind: CommitKind, key: str, *,
              decision_id: str = "") -> CommitVerdict:
        """確定してよいか。**取れるのは1回だけ。**"""
        identifier = str(key or "")
        if not identifier:
            return CommitVerdict(False, kind, "", "missing_id")
        composite = f"{kind}:{identifier}"
        with self._lock:
            if composite in self._committed:
                return CommitVerdict(False, kind, identifier, "already_committed")
            if decision_id:
                # **1つの決定から出せる最終発話は1件。**
                existing = self._by_decision.get(f"{kind}:{decision_id}")
                if existing and existing != identifier:
                    return CommitVerdict(False, kind, identifier,
                                         "decision_already_produced_one")
                self._by_decision[f"{kind}:{decision_id}"] = identifier
            self._remember(composite, decision_id)
        return CommitVerdict(True, kind, identifier, "committed")

    def committed(self, kind: CommitKind, key: str) -> bool:
        with self._lock:
            return f"{kind}:{str(key or '')}" in self._committed

    def claim_chunk(self, tts_job_id: str, index: int) -> bool:
        """ストリーミングの断片。**複数でよいが、同じものは1回。**"""
        marker = f"{tts_job_id}#{int(index)}"
        with self._lock:
            if marker in self._chunks:
                return False
            self._chunks.add(marker)
            return True

    def release(self, kind: CommitKind, key: str) -> None:
        """取り消し（中断など）。**確定していない扱いへ戻す。**"""
        with self._lock:
            self._committed.pop(f"{kind}:{str(key or '')}", None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"committed": len(self._committed),
                    "chunks": len(self._chunks)}


# ---------------------------------------------------------------------------
# 最終防御（根本原因の代わりにはしない）
# ---------------------------------------------------------------------------

#: 意図した繰り返しに見える書き出し。**ここに当たるものは削らない。**
#:
#: 「いや、いや」「少しずつ、少しずつ」のような強調は、同じ語が
#: 並ぶことに意味がある。文の切れ目で判定すると巻き込むので、
#: **文として独立している時だけ**畳む。
_EMPHASIS_MARKERS: tuple[str, ...] = (
    "いや", "まって", "待って", "だめ", "ほんとに", "本当に", "すごい",
    "もっと", "少しずつ", "ちょっと", "ねえ", "ねぇ", "おい", "うそ", "嘘",
)
#: 畳んでよい最大文字数。長い文が偶然似ているのは別の話。
_MAX_COLLAPSE_CHARS = 60


def _emphasis(value: str) -> bool:
    text = normalise(value)
    return any(text.startswith(marker) for marker in _EMPHASIS_MARKERS)


@dataclass(frozen=True, slots=True)
class CollapseResult:
    text: str
    removed: int = 0
    kept_emphasis: int = 0

    @property
    def changed(self) -> bool:
        return self.removed > 0


def collapse_adjacent_duplicates(
    value: str, *, threshold: float = ADJACENT_DUPLICATE,
    already_spoken: int = 0,
) -> CollapseResult:
    """**隣り合う、ほぼ同一の文**だけを1つにする。

    これは最終防御であって、根本原因の修正の代わりではない。
    やらないこと:

    * 離れた文を意味の近さで消す（**別の話をしている可能性がある**）
    * 数字・否定・対象・時制が違う文をまとめる
    * 感情表現や意図的な反復を削る
    * **既に喋り終えた断片を書き換える**

    `already_spoken` に、もう声になった文の数を渡すこと。
    そこまでは触らない。
    """
    items = sentences(value)
    if len(items) < 2:
        return CollapseResult(str(value or ""))

    keep: list[str] = []
    removed = 0
    emphasis = 0
    frozen = max(0, int(already_spoken))

    for index, item in enumerate(items):
        if index < frozen or not keep:
            keep.append(item)
            continue
        previous = keep[-1]
        if similarity(previous, item) < threshold:
            keep.append(item)
            continue
        if len(item) > _MAX_COLLAPSE_CHARS or len(previous) > _MAX_COLLAPSE_CHARS:
            keep.append(item)
            continue
        if _emphasis(item):
            # **強調の繰り返しは残す。**
            keep.append(item)
            emphasis += 1
            continue
        if _differs_in_facts(previous, item):
            keep.append(item)
            continue
        removed += 1

    if not removed:
        return CollapseResult(str(value or ""), 0, emphasis)
    return CollapseResult("。".join(keep) + "。", removed, emphasis)


_DIGITS = re.compile(r"\d+")
_NEGATION = re.compile(r"ない|ません|じゃない|ぬ$|not |n't")
_TENSE = re.compile(r"した|しました|だった|でした|será|will |ing$")


def _differs_in_facts(left: str, right: str) -> bool:
    """数字・否定・時制が違えば、似ていても**別のこと**。"""
    if set(_DIGITS.findall(left)) != set(_DIGITS.findall(right)):
        return True
    if bool(_NEGATION.search(left)) != bool(_NEGATION.search(right)):
        return True
    return bool(_TENSE.search(left)) != bool(_TENSE.search(right))


__all__ = [
    "ADJACENT_DUPLICATE", "CollapseResult", "CommitKind", "CommitVerdict",
    "ConversationKind", "DuplicateSite", "StageRecord", "TextStage",
    "TurnCommitLedger", "TurnFrame", "collapse_adjacent_duplicates",
    "measure", "missing_stages", "normalise", "sentences", "similarity",
    "text_hash",
]
