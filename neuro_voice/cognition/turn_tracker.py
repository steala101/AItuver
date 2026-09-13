"""`TurnFrame` と `TurnCommitLedger` を実経路へ挿す所。

Phase 7C で型もテストも揃っていたのに、**呼び出し側が無かった**。
実装があることと、効いていることは違う——この配線が無い間は、実機で
同じ文が二度聞こえても `TurnMetrics.duplicate_stage` には何も入らず、
「どの層で増えたのか」を次に起きた時も特定できないままだった。

Local (`pipeline.py`) と Discord (`bot.py`) が**同じ物を使う**。
片側だけに置くと、「重複」という同じ言葉が経路ごとに違う意味になる
（AGENTS.md: LocalとDiscordの共通意味論を片側だけ変更しない）。

ここでやらないこと:

* **文章の近さで重複を決めない。** 判定は ID の一意性だけ。
  「うん。うん。」を消すのは `collapse_adjacent_duplicates` の仕事。
* **既定で発話を止めない。** 既定は**記録だけ**
  (`duplicate_suppression_enabled: false`)。原因が分かる前に止めると、
  重複の代わりに欠落が出て、しかも今度は記録も残らない。
* **ID が無い呼び出しを重複扱いにしない。** 素通しする。ここで止めると
  無関係な発話が消える。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from neuro_voice.cognition.turn_integrity import (
    CommitKind,
    CommitVerdict,
    ConversationKind,
    DuplicateSite,
    StageRecord,
    TextStage,
    TurnCommitLedger,
    TurnFrame,
)

logger = logging.getLogger(__name__)

#: 確定に失敗した種別から、どの層で増えたかへの対応。
_SITE_BY_KIND: dict[str, DuplicateSite] = {
    str(CommitKind.SPEECH_REQUEST): DuplicateSite.SPEECH_REQUEST_DUPLICATE,
    str(CommitKind.TTS_JOB): DuplicateSite.TTS_JOB_DUPLICATE,
    str(CommitKind.PLAYBACK): DuplicateSite.PLAYBACK_SEGMENT,
}

#: 無効時に返す理由。**「確定した」ではなく「見ていない」。**
DISABLED = "tracker_disabled"
#: ID が無くて判定できなかった。**重複ではない。**
PASSTHROUGH = "missing_id_passthrough"

#: 覚えておく重複の件数。診断表示に出すだけなので少なくてよい。
_DUPLICATE_LOG = 64


class TurnTracker:
    """1ターンを追う入れ物と、二度確定させない台帳をまとめて持つ。

    `_on_play_start` は再生スレッドから呼ばれる。**別スレッドから
    触られる前提**で鍵を持つ。
    """

    def __init__(self, *, enabled: bool = False,
                 suppress_duplicates: bool = False,
                 capacity: int = 64, ledger_capacity: int = 256) -> None:
        self._enabled = bool(enabled)
        self._suppress = bool(suppress_duplicates)
        self._capacity = max(1, int(capacity))
        self._ledger = TurnCommitLedger(capacity=int(ledger_capacity))
        self._frames: OrderedDict[str, TurnFrame] = OrderedDict()
        self._duplicates: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    # ---------- 状態 ----------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def suppressing(self) -> bool:
        """重複を**止める**か。記録するだけの状態と区別する。"""
        return self._enabled and self._suppress

    # ---------- ターン ----------

    def begin(self, metrics: Any, *,
              kind: ConversationKind | str = ConversationKind.NORMAL_CONVERSATION,
              input_event_id: str = "") -> TurnFrame | None:
        """ターンの入口。**同じ `turn_id` で2回呼んでも1件しか作らない。**

        作り直すと、後半の段階が別のターンとして記録され、結局
        「どこで増えたか」が追えなくなる（Phase 7D 第2項）。呼び出し側が
        入口を1箇所に絞れない事情（直接テキスト・自発発話・内部イベント）
        があるので、**冪等にしてある方が安全**。
        """
        if metrics is None or not self._enabled:
            return None
        turn_id = str(getattr(metrics, "turn_id", "") or "")
        if not turn_id:
            return None
        with self._lock:
            existing = self._frames.get(turn_id)
            if existing is not None:
                return existing
            frame = TurnFrame(
                turn_id=turn_id,
                kind=kind,
                session_id=str(getattr(metrics, "session_id", "") or ""),
                active_persona_id=str(getattr(metrics, "persona_id", "") or ""),
                active_persona_version=str(
                    getattr(metrics, "persona_version", "") or "1"),
                persona_epoch=int(getattr(metrics, "persona_epoch", 0) or 0),
                effective_cognition_enabled=bool(getattr(
                    metrics, "effective_cognition_enabled", False)),
                rollout_mode=str(getattr(metrics, "cognition_rollout_mode", "disabled")),
                requested_rollout_mode=str(getattr(
                    metrics, "cognition_requested_rollout_mode", "disabled")),
                cognition_execution_path=str(getattr(
                    metrics, "cognition_execution_path", "legacy")),
                cognition_session_epoch=int(getattr(
                    metrics, "cognition_session_epoch", 0) or 0),
                cognition_activation_source=str(getattr(
                    metrics, "cognition_activation_source", "config")),
                cognition_config_fingerprint=str(getattr(
                    metrics, "cognition_config_fingerprint", "")),
                transport=str(getattr(metrics, "transport", "LOCAL")),
                input_event_id=str(
                    input_event_id or getattr(metrics, "input_event_id", "") or ""),
            )
            self._frames[turn_id] = frame
            while len(self._frames) > self._capacity:
                self._frames.popitem(last=False)
            return frame

    def frame(self, metrics: Any) -> TurnFrame | None:
        """`TurnMetrics` でも `turn_id` の文字列でも引ける。"""
        if metrics is None:
            return None
        turn_id = (metrics if isinstance(metrics, str)
                   else str(getattr(metrics, "turn_id", "") or ""))
        if not turn_id:
            return None
        with self._lock:
            return self._frames.get(turn_id)

    def note_text(self, metrics: Any, stage: TextStage | str, text: str, *,
                  once: bool = True) -> StageRecord | None:
        """段階ごとの文章を**指紋だけ**記録する（本文は残さない）。

        `once` が既定で真なのは、ストリーミングだと同じ段階が文の数だけ
        来るから。全部足すと、30文のターンで段階記録が 30 行になり、
        肝心の「段階間で文数が変わった所」が読めなくなる。
        """
        frame = self.frame(metrics)
        if frame is None:
            return None
        with self._lock:
            if once and frame.stage(stage) is not None:
                return None
            record = frame.note_text(stage, text)
        if (str(stage) == str(TextStage.LLM_OUTPUT)
                and record.duplicate_adjacent_pairs):
            # **生成そのものが繰り返している。** ここが真なら、
            # 下流をいくら見ても直す場所は見つからない。
            self._record_duplicate(
                metrics, DuplicateSite.LLM_GENERATION_DUPLICATE, {
                    "stage": str(stage),
                    "duplicate_pairs": record.duplicate_adjacent_pairs,
                    "max_adjacent_similarity": round(
                        record.max_adjacent_similarity, 3),
                })
        return record

    # ---------- 一意性 ----------

    def commit(self, metrics: Any, kind: CommitKind, key: str, *,
               decision_id: str = "") -> CommitVerdict:
        """確定してよいか。**取れるのは1回だけ。**"""
        if not self._enabled:
            return CommitVerdict(True, kind, str(key or ""), DISABLED)
        verdict = self._ledger.claim(kind, key, decision_id=decision_id)
        if verdict.accepted:
            self._note_delivery(metrics, kind, accepted=True)
            self._bind_id(metrics, kind, verdict.key)
            return verdict
        if verdict.reason == "missing_id":
            self._note_delivery(metrics, kind, accepted=True)
            return CommitVerdict(True, kind, "", PASSTHROUGH)
        self._note_delivery(metrics, kind, accepted=False)
        self._record_duplicate(
            metrics, _SITE_BY_KIND.get(str(kind), DuplicateSite.NONE), {
                "kind": str(kind), "key": verdict.key,
                "reason": verdict.reason,
                "decision_id": str(decision_id or ""),
            })
        return verdict

    def commit_chunk(self, metrics: Any, tts_job_id: str,
                     index: int) -> CommitVerdict:
        """ストリーミングの断片。**何個あってもよいが、同じ番号は1回。**

        番号は発話ループの呼び出しごとに 0 から数える。同じ応答に対して
        発話ループが2つ立ち上がると、**両方が 0 番から始まる**ので
        ここで気づける。断片ごとに新しい ID を振ると、この検査は常に
        通ってしまい何も見ていないのと同じになる。
        """
        if not self._enabled:
            return CommitVerdict(True, CommitKind.TTS_JOB, "", DISABLED)
        identifier = str(tts_job_id or "")
        if not identifier:
            return CommitVerdict(True, CommitKind.TTS_JOB, "", PASSTHROUGH)
        marker = f"{identifier}#{int(index)}"
        if self._ledger.claim_chunk(identifier, index):
            self._note_delivery(metrics, CommitKind.TTS_JOB, accepted=True)
            return CommitVerdict(True, CommitKind.TTS_JOB, marker, "committed")
        self._note_delivery(metrics, CommitKind.TTS_JOB, accepted=False)
        self._record_duplicate(
            metrics, DuplicateSite.TTS_JOB_DUPLICATE, {
                "kind": str(CommitKind.TTS_JOB), "key": marker,
                "reason": "chunk_already_committed",
            })
        return CommitVerdict(False, CommitKind.TTS_JOB, marker,
                             "chunk_already_committed")

    def note_tts_chunk(
        self, metrics: Any, *, speech_request_id: str, tts_job_id: str,
        chunk_index: int, text_hash: str, audio_hash: str,
    ) -> None:
        """Keep one privacy-safe TTS record for an already accepted chunk."""
        frame = self.frame(metrics)
        if frame is None or not tts_job_id:
            return
        with self._lock:
            details = self._delivery_details(frame)
            jobs = details["tts_jobs"]
            if tts_job_id in jobs:
                return
            jobs[tts_job_id] = {
                "tts_job_id": str(tts_job_id),
                "speech_request_id": str(speech_request_id),
                "chunk_index": int(chunk_index),
                "text_hash": str(text_hash),
                "audio_hash": str(audio_hash),
            }

    def start_playback_segment(
        self, metrics: Any, *, speech_request_id: str, playback_session_id: str,
        playback_id: str, segment_index: int, tts_job_id: str,
        text_hash: str, audio_hash: str,
    ) -> CommitVerdict:
        """Start one segment under exactly one logical playback session.

        A streaming response may have many distinct segments.  A different
        session for the same SpeechRequest, or the same segment ID again, is
        a real duplicate and is rejected even while broad duplicate
        suppression is only observing.
        """
        frame = self.frame(metrics)
        if frame is None or not playback_id:
            return CommitVerdict(True, CommitKind.PLAYBACK, str(playback_id or ""),
                                 PASSTHROUGH)
        speech_id = str(speech_request_id or "")
        session_id = str(playback_session_id or "")
        now = time.time()
        with self._lock:
            details = self._delivery_details(frame)
            existing_session = details["session_by_speech_request"].get(speech_id)
            if existing_session and existing_session != session_id:
                details["replayed_session_count"] += 1
                self._record_duplicate(metrics, DuplicateSite.PLAYBACK_SESSION, {
                    "kind": "playback_session", "speech_request_id": speech_id,
                    "playback_session_id": session_id,
                    "existing_playback_session_id": existing_session,
                    "reason": "speech_request_already_has_session",
                })
                return CommitVerdict(False, CommitKind.PLAYBACK, session_id,
                                     "speech_request_already_has_session")
            if not existing_session:
                details["session_by_speech_request"][speech_id] = session_id
                details["sessions"][session_id] = {
                    "playback_session_id": session_id,
                    "speech_request_id": speech_id,
                    "started_at": 0.0,
                    "ended_at": 0.0,
                    "sealed": False,
                }
            fingerprint = (session_id, int(segment_index), str(audio_hash))
            existing_segment = details["segment_by_fingerprint"].get(fingerprint)
            if existing_segment:
                details["replayed_segment_count"] += 1
                self._note_delivery(metrics, CommitKind.PLAYBACK, accepted=False)
                self._record_duplicate(metrics, DuplicateSite.PLAYBACK_SEGMENT, {
                    "kind": "playback_segment", "playback_id": str(playback_id),
                    "existing_playback_id": existing_segment,
                    "playback_session_id": session_id,
                    "reason": "segment_index_and_audio_hash_already_accepted",
                })
                return CommitVerdict(False, CommitKind.PLAYBACK, str(playback_id),
                                     "segment_index_and_audio_hash_already_accepted")
            verdict = self._ledger.claim(CommitKind.PLAYBACK, playback_id)
            if not verdict.accepted:
                details["replayed_segment_count"] += 1
                self._note_delivery(metrics, CommitKind.PLAYBACK, accepted=False)
                self._record_duplicate(metrics, DuplicateSite.PLAYBACK_SEGMENT, {
                    "kind": "playback_segment", "playback_id": verdict.key,
                    "playback_session_id": session_id,
                    "reason": verdict.reason,
                })
                return verdict
            self._note_delivery(metrics, CommitKind.PLAYBACK, accepted=True)
            self._bind_id(metrics, CommitKind.PLAYBACK, verdict.key)
            details["segments"][verdict.key] = {
                "playback_id": verdict.key,
                "playback_session_id": session_id,
                "speech_request_id": speech_id,
                "tts_job_id": str(tts_job_id),
                "segment_index": int(segment_index),
                "text_hash": str(text_hash),
                "audio_hash": str(audio_hash),
                "started_at": now,
                "ended_at": 0.0,
                "completed": False,
            }
            details["segment_by_fingerprint"][fingerprint] = verdict.key
            return verdict

    def mark_playback_segment_started(self, metrics: Any, playback_id: str) -> None:
        """Record actual device-start time after an already accepted enqueue."""
        frame = self.frame(metrics)
        if frame is None or not playback_id:
            return
        with self._lock:
            details = self._delivery_details(frame)
            segment = details["segments"].get(str(playback_id))
            if segment is None:
                return
            now = time.time()
            segment["started_at"] = now
            session = details["sessions"].get(str(segment["playback_session_id"]))
            if session is not None:
                previous = float(session.get("started_at") or 0.0)
                session["started_at"] = now if previous <= 0 else min(previous, now)

    def complete_playback_segment(self, metrics: Any, playback_id: str,
                                  *, completed: bool) -> None:
        frame = self.frame(metrics)
        if frame is None or not playback_id:
            return
        with self._lock:
            segment = self._delivery_details(frame)["segments"].get(str(playback_id))
            if segment is None:
                return
            segment["ended_at"] = time.time()
            segment["completed"] = bool(completed)
            session = self._delivery_details(frame)["sessions"].get(
                str(segment["playback_session_id"]))
            if session is not None and self._session_complete(session, self._delivery_details(frame)):
                session["ended_at"] = float(segment["ended_at"])

    def seal_playback_session(self, metrics: Any, playback_session_id: str) -> None:
        frame = self.frame(metrics)
        if frame is None or not playback_session_id:
            return
        with self._lock:
            details = self._delivery_details(frame)
            session = details["sessions"].get(str(playback_session_id))
            if session is not None:
                session["sealed"] = True
                if self._session_complete(session, details):
                    session["ended_at"] = time.time()

    def seal_all_playback_sessions(self, metrics: Any) -> None:
        """Close enqueue for all sessions belonging to this turn."""
        frame = self.frame(metrics)
        if frame is None:
            return
        with self._lock:
            details = self._delivery_details(frame)
            for session in details["sessions"].values():
                session["sealed"] = True
                if self._session_complete(session, details):
                    session["ended_at"] = time.time()

    def playback_finalized(self, metrics: Any) -> bool:
        frame = self.frame(metrics)
        if frame is None:
            return True
        with self._lock:
            details = self._delivery_details(frame)
            sessions = list(details["sessions"].values())
            return not sessions or all(
                bool(session.get("sealed")) and self._session_complete(session, details)
                for session in sessions
            )

    def release(self, kind: CommitKind, key: str) -> None:
        """中断されたので確定を取り消す。

        途中で止まった再生を確定のまま残すと、**再開した時に同じ音が
        「もう鳴らした」ことになって二度と鳴らない**。重複を嫌うあまり
        欠落を作らないための戻し口。
        """
        if not self._enabled or not key:
            return
        self._ledger.release(kind, str(key))

    def blocks(self, verdict: CommitVerdict | None) -> bool:
        """止めてよいか。**既定は止めない。** 記録して素通しする。"""
        return bool(self.suppressing and verdict is not None
                    and not verdict.accepted)

    # ---------- 記録 ----------

    def _note_delivery(self, metrics: Any, kind: CommitKind, *, accepted: bool) -> None:
        """Trace 用に経路別件数だけを残す。本文や ID は記録しない。"""
        frame = self.frame(metrics)
        if frame is None:
            return
        name = str(kind)
        with self._lock:
            counts = frame.delivery_counts.setdefault(
                name, {"attempted": 0, "accepted": 0, "rejected": 0},
            )
            counts["attempted"] += 1
            counts["accepted" if accepted else "rejected"] += 1

    def delivery_snapshot(self, metrics: Any) -> dict[str, Any]:
        """SpeechRequest → TTS → Playback の安全な診断情報。"""
        frame = self.frame(metrics)
        with self._lock:
            stages = (frame.delivery_counts if frame is not None else {})
            stage_snapshots = {name: dict(value) for name, value in stages.items()}
            details = self._delivery_details(frame) if frame is not None else self._empty_delivery_details()
            sessions = [dict(item) for item in details["sessions"].values()]
            segments = [dict(item) for item in details["segments"].values()]
            tts_jobs = [dict(item) for item in details["tts_jobs"].values()]
        unique_keys = {
            str(item.get("audio_hash") or item.get("playback_id")) for item in segments
        }
        return {
            "speech_request": stage_snapshots.get(str(CommitKind.SPEECH_REQUEST), {}),
            "tts_job": stage_snapshots.get(str(CommitKind.TTS_JOB), {}),
            "playback": stage_snapshots.get(str(CommitKind.PLAYBACK), {}),
            "speech_request_count": int(stage_snapshots.get(str(CommitKind.SPEECH_REQUEST), {}).get("accepted", 0)),
            "tts_chunk_count": int(stage_snapshots.get(str(CommitKind.TTS_JOB), {}).get("accepted", 0)),
            "logical_playback_session_count": len(sessions),
            "playback_segment_count": len(segments),
            "unique_playback_segment_count": len(segments),
            "replayed_segment_count": int(details["replayed_segment_count"]),
            "speech_request_ids": ([str(frame.speech_request_id)] if frame is not None
                                   and frame.speech_request_id else []),
            "tts_jobs": tts_jobs[:16],
            "playback_sessions": sessions[:8],
            "playback_segments": segments[:32],
            "overlap_detected": self._has_overlap(segments),
            "first_duplicate_stage": str(
                getattr(metrics, "duplicate_stage", "") or "none"),
        }

    @staticmethod
    def _empty_delivery_details() -> dict[str, Any]:
        return {
            "tts_jobs": {}, "sessions": {}, "segments": {},
            "session_by_speech_request": {}, "segment_by_fingerprint": {},
            "replayed_segment_count": 0,
            "replayed_session_count": 0,
        }

    def _delivery_details(self, frame: TurnFrame | None) -> dict[str, Any]:
        if frame is None:
            return self._empty_delivery_details()
        if not frame.delivery_details:
            frame.delivery_details = self._empty_delivery_details()
        return frame.delivery_details

    @staticmethod
    def _session_complete(session: dict[str, Any], details: dict[str, Any]) -> bool:
        session_id = str(session.get("playback_session_id", ""))
        segments = [item for item in details["segments"].values()
                    if str(item.get("playback_session_id", "")) == session_id]
        return bool(segments) and all(float(item.get("ended_at", 0.0)) > 0 for item in segments)

    @staticmethod
    def _has_overlap(segments: list[dict[str, Any]]) -> bool:
        ordered = sorted(
            (item for item in segments if float(item.get("started_at", 0.0)) > 0),
            key=lambda item: float(item["started_at"]),
        )
        latest_end = 0.0
        for item in ordered:
            started = float(item.get("started_at", 0.0))
            if latest_end and started < latest_end:
                return True
            latest_end = max(latest_end, float(item.get("ended_at", 0.0)))
        return False

    def _bind_id(self, metrics: Any, kind: CommitKind, key: str) -> None:
        frame = self.frame(metrics)
        if frame is None or not key:
            return
        attribute = {
            str(CommitKind.SPEECH_REQUEST): "speech_request_id",
            str(CommitKind.TTS_JOB): "tts_job_id",
            str(CommitKind.PLAYBACK): "playback_commit_id",
        }.get(str(kind))
        if attribute is None:
            return
        with self._lock:
            if not getattr(frame, attribute, ""):
                setattr(frame, attribute, key)

    def _record_duplicate(self, metrics: Any, site: DuplicateSite,
                          detail: dict[str, Any]) -> None:
        turn_id = "" if metrics is None else str(
            getattr(metrics, "turn_id", "") or "")
        entry = {"turn_id": turn_id, "site": str(site), **detail}
        with self._lock:
            self._duplicates.append(entry)
            del self._duplicates[:-_DUPLICATE_LOG]
        if metrics is not None and not str(
                getattr(metrics, "duplicate_stage", "") or ""):
            # **最初に見つかった段階を残す。** 上流ほど先に起きるので、
            # 後から上書きすると、生成が繰り返していたターンが
            # 「再生の問題」に見えてしまう（`duplicate_site()` と同じ順序）。
            metrics.duplicate_stage = str(site)
            metrics.duplicate_detail = dict(entry)
        logger.warning("ターンの重複を検出: %s %s", site, entry)

    def duplicates(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._duplicates)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            frames = len(self._frames)
            duplicates = len(self._duplicates)
            latest = self._duplicates[-1] if self._duplicates else None
        return {
            "enabled": self._enabled, "suppressing": self.suppressing,
            "frames": frames, "duplicates": duplicates,
            "latest_duplicate": latest,
            "ledger": self._ledger.snapshot(),
        }


def tracker_from_config(cfg: Any) -> TurnTracker:
    """設定から1つ作る。**既定は無効**（`config/config.yaml` と同じ）。"""

    def _get(name: str, fallback: Any) -> Any:
        try:
            return cfg.get(name, fallback)
        except Exception:
            return fallback

    return TurnTracker(
        enabled=bool(_get("turn_integrity.turn_frame_enabled", False)),
        suppress_duplicates=bool(
            _get("turn_integrity.duplicate_suppression_enabled", False)),
        capacity=int(_get("turn_integrity.frame_capacity", 64) or 64),
        ledger_capacity=int(_get("turn_integrity.commit_capacity", 256) or 256),
    )


__all__ = ["DISABLED", "PASSTHROUGH", "TurnTracker", "tracker_from_config"]
