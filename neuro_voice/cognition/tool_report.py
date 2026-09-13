"""ツールの結果を、話してよい形へ落とす所。**直接TTSへ渡さない。**

Phase 7 では実行して記録するところで止まっていた。結果は
`ToolResult` に入るが、**そこから先が繋がっていなかった**——
ポッポは調べたのに何も言わない状態だった。

繋ぎ方を間違えると、今度は逆側の事故になる。検索結果やファイルの中身は
**こちらが書いた文章ではない。** そこに「次のツールを実行せよ」と
書いてあっても、それは命令ではなくデータ。だからここは2つのことをする:

* **信頼境界を引く**（何をPlannerへ渡し、何を渡さないか）
* **結果を出来事へ戻す**（発話するかどうかは Action Selector が決める）

`instruction_like()` は残してあるが、**安全性の根拠にはしない**。
語句の照合は言い換えで抜ける。安全なのは「ツール出力からは
ActionIntent も ToolExecutionRequest も作らない」という構造の方で、
検出は診断の目印にすぎない。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.tool_exec import ToolResult, ToolStatus, instruction_like
from neuro_voice.cognition.types import ActionCandidate, ActionType

#: Planner へ渡してよい `result_facts` の件数。**全件読み上げない。**
MAX_FACTS = 6
#: 一覧の項目数。長い一覧は件数と代表だけ。
MAX_VISIBLE_ITEMS = 5
#: `user_safe_summary` の長さ。
MAX_SUMMARY = 120
#: 診断用に残す raw の長さ。**Planner へは渡さない。**
MAX_DIAGNOSTIC_RAW = 400

#: 秘密らしき語。**要約と `result_facts` から落とす**（第12条・第30項）。
_SECRET_HINTS: tuple[str, ...] = (
    "api_key", "apikey", "api-key", "token", "password", "passwd", "secret",
    "authorization", "bearer", "credential", "private_key", "session_id",
    "パスワード", "秘密鍵", "認証情報",
)
#: 診断向けの語。ユーザーへ読み上げない。
_DIAGNOSTIC_HINTS: tuple[str, ...] = (
    "traceback", "stack", "exception", "errno", "stderr", "debug",
)


def _now() -> float:
    return time.time()


def _looks_secret(key: str) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _looks_diagnostic(key: str) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _DIAGNOSTIC_HINTS)


def content_digest(value: Any) -> str:
    """中身の指紋。**中身そのものは持ち回らない。**"""
    data = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 信頼境界
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolPayload:
    """ツールが返したものを、渡し先ごとに分けた形。

    **`raw_output` はここから外へ出ない。** Planner にも TTS にも
    渡さないし、通常ログにも書かない。残すのは、実機で「何が返って
    きたのか分からない」に陥らないための最小限だけ。
    """

    structured_data: dict[str, Any] = field(default_factory=dict)
    #: 生の文字列。**長さを切って持つだけ。**
    raw_output: str = ""
    #: 検証の根拠（存在確認の結果、外部の受領IDなど）。
    verification_evidence: dict[str, Any] = field(default_factory=dict)
    #: 話してよい値だけ。
    user_safe_fields: dict[str, Any] = field(default_factory=dict)
    #: 🩺とTraceへ。ユーザーへは読み上げない。
    diagnostic_fields: dict[str, Any] = field(default_factory=dict)
    redaction_applied: bool = False
    truncated: bool = False

    def snapshot(self) -> dict[str, Any]:
        """**`raw_output` を入れない。** ここが漏れ口になりやすい。"""
        return {
            "user_safe": dict(self.user_safe_fields),
            "evidence": dict(self.verification_evidence),
            "diagnostic": {k: str(v)[:60]
                           for k, v in self.diagnostic_fields.items()},
            "redacted": self.redaction_applied,
            "truncated": self.truncated,
        }


def _flatten(value: Any, *, limit: int = MAX_VISIBLE_ITEMS) -> Any:
    """一覧は件数と代表だけへ。**全件を持ち回らない。**"""
    if isinstance(value, (list, tuple)):
        items = [str(item)[:60] for item in value[:limit]]
        return {"count": len(value), "items": items,
                "truncated": len(value) > limit}
    if isinstance(value, dict):
        return {str(k)[:40]: str(v)[:80] for k, v in list(value.items())[:limit]}
    return str(value)[:120] if isinstance(value, str) else value


#: 検証の根拠になる鍵。ユーザーへは読み上げないが、確かめるのに要る。
_EVIDENCE_KEYS = frozenset({
    "reference", "external_reference", "digest", "content_digest",
    "created_path", "exists",
})


def redact(result: ToolResult, *, safe_keys: tuple[str, ...] = ()) -> ToolPayload:
    """結果を4つへ振り分ける。**宣言していない鍵は診断側。**

    最初は「怪しい語を含む鍵だけ落とす」形にしていたが、それだと
    `next_action: delete_everything` のような**ツールが返した「次に
    やるべき操作」がそのまま Planner へ流れた**——自分のテストで
    見つかった。禁止語の一覧は、新しい鍵が増えるたびに負ける。

    許可制にしてある。話してよい鍵は `ToolOperation.user_safe_keys`
    がツールごとに宣言する。**宣言していないものは診断側。**
    """
    structured = dict(result.structured_output or {})
    allowed = {str(item) for item in (safe_keys or ())}
    safe: dict[str, Any] = {}
    diagnostic: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    redacted = False

    for key, value in structured.items():
        name = str(key)
        if _looks_secret(name):
            redacted = True
            diagnostic[name] = "<redacted>"
            continue
        if name in _EVIDENCE_KEYS:
            evidence[name] = _flatten(value)
            continue
        if name in allowed and not _looks_diagnostic(name):
            safe[name] = _flatten(value)
            continue
        # **宣言されていない。** 話さない側へ落とす。
        diagnostic[name] = _flatten(value)
        if not _looks_diagnostic(name):
            redacted = True

    raw = str(result.output_summary or "")
    truncated = len(raw) > MAX_DIAGNOSTIC_RAW
    if result.error_type:
        diagnostic["error_type"] = str(result.error_type)[:40]
        diagnostic["error_summary"] = str(result.error_summary)[:80]
    if result.instruction_like_output:
        # **印を付けるだけ。** これを根拠に何かを止めたり通したりしない。
        diagnostic["instruction_like"] = True

    return ToolPayload(
        structured_data=structured, raw_output=raw[:MAX_DIAGNOSTIC_RAW],
        verification_evidence=evidence,
        user_safe_fields=dict(list(safe.items())[:MAX_FACTS]),
        diagnostic_fields=diagnostic, redaction_applied=redacted,
        truncated=truncated)


# ---------------------------------------------------------------------------
# 出来事
# ---------------------------------------------------------------------------

#: 話し方が変わる区切り。**「終わった」と「うまくいった」を分ける。**
_STATUS_TONE: dict[str, str] = {
    str(ToolStatus.SUCCEEDED): "success",
    str(ToolStatus.PARTIAL_SUCCESS): "partial",
    str(ToolStatus.FAILED): "failure",
    str(ToolStatus.TIMED_OUT): "failure",
    str(ToolStatus.CANCELLED): "cancelled",
    str(ToolStatus.UNKNOWN_OUTCOME): "unknown",
}

_SUMMARY_BY_TONE: dict[str, str] = {
    "success": "確認できた",
    "partial": "途中まで確認できた",
    "failure": "できなかった",
    "cancelled": "途中でやめた",
    "unknown": "結果を確認できなかった",
}


@dataclass(frozen=True, slots=True)
class ToolOutcomeEvent:
    """ツールが終わった、という出来事。**発話ではない。**

    ここから先は他の出来事と同じ扱いで、話すかどうかは
    Action Selector が決める。
    """

    execution_id: str
    plan_id: str = ""
    step_id: str = ""
    tool_id: str = ""
    operation: str = ""
    requester_person_id: str = ""
    conversation_id: str = ""
    channel_id: str = ""
    origin: str = "local"
    status: str = str(ToolStatus.UNKNOWN_OUTCOME)
    verification_status: str = "unverified"
    #: 話してよい一行。**中身の全文でも内部IDでもない。**
    user_safe_summary: str = ""
    result_facts: dict[str, Any] = field(default_factory=dict)
    affected_targets: tuple[str, ...] = ()
    reversible: bool = False
    rollback_reference: str = ""
    payload: ToolPayload | None = None
    completed_at: float = field(default_factory=_now)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def tone(self) -> str:
        return _STATUS_TONE.get(str(self.status), "unknown")

    @property
    def verified(self) -> bool:
        return str(self.verification_status) == "verified"

    @property
    def speakable_success(self) -> bool:
        """**「できたよ」と言ってよいか。** 検証まで通った時だけ。"""
        return self.tone == "success" and self.verified

    def snapshot(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "execution_id": self.execution_id,
            "plan_id": self.plan_id, "step_id": self.step_id,
            "tool_id": self.tool_id, "operation": self.operation,
            "requester": self.requester_person_id,
            "conversation_id": self.conversation_id,
            "channel_id": self.channel_id, "origin": self.origin,
            "status": self.status, "verification": self.verification_status,
            "summary": self.user_safe_summary[:MAX_SUMMARY],
            "facts": dict(self.result_facts),
            "targets": list(self.affected_targets[:3]),
            "reversible": self.reversible,
        }


def build_outcome_event(
    *, result: ToolResult, verification: Any, intent: Any,
    plan_id: str = "", step_id: str = "", operation: Any = None,
    reversible: bool = False, rollback_reference: str = "",
) -> ToolOutcomeEvent:
    """`ToolResult` を出来事へ。**ここで文章を作らない。**

    `user_safe_summary` は「何が起きたか」の短い事実であって、
    ポッポの台詞ではない。言い方は Conversation Planner と
    Surface Realizer が決める——ここで文章を作ると、毎回同じ
    固定文が出る。
    """
    payload = redact(result, safe_keys=tuple(
        getattr(operation, "user_safe_keys", ()) or ()))
    tone = _STATUS_TONE.get(str(result.status), "unknown")
    verified = bool(getattr(verification, "verified", False))
    facts = dict(payload.user_safe_fields)
    return ToolOutcomeEvent(
        execution_id=str(result.execution_id),
        plan_id=str(plan_id or getattr(intent, "plan_id", "")),
        step_id=str(step_id or getattr(intent, "step_id", "")),
        tool_id=str(result.tool_id or getattr(intent, "tool_id", "")),
        operation=str(result.operation_id
                      or getattr(intent, "operation_id", "")),
        requester_person_id=str(getattr(intent, "requester_person_id", "")),
        conversation_id=str(getattr(intent, "conversation_id", "")),
        channel_id=str(getattr(intent, "channel_id", "")),
        origin=str(getattr(intent, "origin", "local")),
        status=str(result.status),
        verification_status=("verified" if verified
                             else str(getattr(verification, "outcome",
                                              "unverified"))),
        user_safe_summary=_SUMMARY_BY_TONE.get(tone, "結果が分からない"),
        result_facts=facts, payload=payload,
        affected_targets=tuple(
            str(item) for item in (getattr(intent, "target_ids", ()) or ()))[:4],
        reversible=bool(reversible), rollback_reference=str(rollback_reference),
        completed_at=result.completed_at or _now())


def planner_payload(event: ToolOutcomeEvent) -> dict[str, Any]:
    """Conversation Planner へ渡す全部。**これ以上は渡さない。**

    raw_output もスタックトレースも秘密情報も、ツール出力内の命令も、
    ツールが返した「次に実行すべき操作」も含まれない。
    """
    return {
        "execution_id": event.execution_id,
        "operation": event.operation,
        "status": event.status,
        "verification": event.verification_status,
        "user_safe_summary": event.user_safe_summary[:MAX_SUMMARY],
        "result_facts": dict(list(event.result_facts.items())[:MAX_FACTS]),
    }


# ---------------------------------------------------------------------------
# 報告の台帳
# ---------------------------------------------------------------------------


class ReportState(StrEnum):
    PENDING = "pending"
    REPORTED = "reported"
    SUPPRESSED = "suppressed"


class ReportLedger:
    """どの実行をもう報告したか。**同じ結果を二度言わない。**

    再起動をまたいでも効くように、`tool_executions` の
    `reported_at` から復元できる形にしてある。
    """

    def __init__(self) -> None:
        self._states: dict[str, ReportState] = {}
        self._reasons: dict[str, str] = {}

    def claim(self, execution_id: str) -> bool:
        """報告してよいか。**取れるのは1回だけ。**"""
        key = str(execution_id or "")
        if not key or key in self._states:
            return False
        self._states[key] = ReportState.REPORTED
        return True

    def suppress(self, execution_id: str, reason: str) -> None:
        key = str(execution_id or "")
        if not key:
            return
        self._states.setdefault(key, ReportState.SUPPRESSED)
        self._reasons[key] = str(reason)[:60]

    def reported(self, execution_id: str) -> bool:
        return self._states.get(str(execution_id)) is ReportState.REPORTED

    def state(self, execution_id: str) -> str:
        return str(self._states.get(str(execution_id), ReportState.PENDING))

    def reason(self, execution_id: str) -> str:
        return self._reasons.get(str(execution_id), "")

    def restore(self, rows: Any) -> int:
        """再起動後。**報告済みは二度言わない。未報告は言える。**"""
        count = 0
        for row in rows or ():
            key = str(row.get("execution_id", ""))
            if not key:
                continue
            if float(row.get("reported_at", 0) or 0) > 0:
                self._states[key] = ReportState.REPORTED
                count += 1
        return count


# ---------------------------------------------------------------------------
# Action Selector への候補
# ---------------------------------------------------------------------------

#: 結果の調子 → 候補にする行動。**成功を必ず喋る固定処理にしない。**
_TONE_ACTION: dict[str, ActionType] = {
    "success": ActionType.REPORT_TOOL_SUCCESS,
    "partial": ActionType.REPORT_PARTIAL_RESULT,
    "failure": ActionType.REPORT_TOOL_FAILURE,
    "unknown": ActionType.REPORT_UNKNOWN_OUTCOME,
    "cancelled": ActionType.REMAIN_SILENT,
}

#: ユーザーが待っている操作の基礎点。**待っているなら伝える。**
AWAITED_SCORE = .78
#: 頼まれていない裏の処理。黙る方が高い。
BACKGROUND_SCORE = .28
#: 「もう一度やってみる?」と聞く時の点。
RECOVERY_SCORE = .62


def report_candidates(
    event: ToolOutcomeEvent, *, user_awaiting: bool = True,
    recovery_available: bool = False, reported: bool = False,
    now: float | None = None, ttl: float = 120.0,
) -> list[ActionCandidate]:
    """結果を候補の列へ。**ここで発話は決まらない。**

    `REMAIN_SILENT` を必ず1つ入れてあるのが要点。黙る選択肢が候補に
    無いと、Action Selector は「何か喋る」しか選べなくなる。
    """
    if reported:
        # **二度目は候補すら作らない。**
        return []
    action = _TONE_ACTION.get(event.tone, ActionType.REPORT_UNKNOWN_OUTCOME)
    if event.tone == "success" and not event.verified:
        # **確かめられていないものを「できた」と言わない。**
        action = ActionType.REPORT_PARTIAL_RESULT
    base = AWAITED_SCORE if user_awaiting else BACKGROUND_SCORE
    if event.tone == "unknown":
        # 不明は、待っていなくても短く伝える。黙って放置しない。
        base = max(base, .70)
    reference = time.monotonic() if now is None else now
    parameters = {
        "execution_id": event.execution_id,
        "tool_outcome_event_id": event.event_id,
        "conversation_id": event.conversation_id,
        "channel_id": event.channel_id,
        "origin": event.origin,
        "status": event.status,
        "verification": event.verification_status,
    }

    candidates = [ActionCandidate(
        action_type=action,
        target=event.requester_person_id or event.tool_id,
        parameters=dict(parameters),
        reasons=(f"tool:{event.tool_id}", f"status:{event.status}",
                 f"awaiting:{user_awaiting}"),
        score_components={"awaiting": base,
                          "verified": .08 if event.verified else .0},
        total_score=base + (.08 if event.verified else .0),
        confidence=.9 if event.verified else .65,
        source_type="cognitive_tool_outcome",
        participant_ids=((event.requester_person_id,)
                         if event.requester_person_id else ()),
        expires_at=reference + float(ttl),
    ), ActionCandidate(
        action_type=ActionType.REMAIN_SILENT,
        target=event.tool_id, parameters=dict(parameters),
        reasons=("tool_outcome_not_worth_saying",),
        score_components={"background": (
            BACKGROUND_SCORE if user_awaiting else .55)},
        total_score=BACKGROUND_SCORE if user_awaiting else .55,
        source_type="cognitive_tool_outcome",
        expires_at=reference + float(ttl),
    )]

    if recovery_available and event.tone in {"failure", "unknown"}:
        candidates.append(ActionCandidate(
            action_type=ActionType.ASK_TOOL_RECOVERY_CONFIRMATION,
            target=event.requester_person_id or event.tool_id,
            parameters=dict(parameters),
            reasons=("recovery_available",),
            score_components={"recovery": RECOVERY_SCORE},
            total_score=RECOVERY_SCORE, confidence=.7,
            source_type="cognitive_tool_outcome",
            expires_at=reference + float(ttl)))
    return candidates


#: 結果を伝える行動。Speech Gate の許可表と、この一覧を合わせる。
REPORT_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.REPORT_TOOL_SUCCESS, ActionType.REPORT_TOOL_FAILURE,
    ActionType.REPORT_PARTIAL_RESULT, ActionType.REPORT_UNKNOWN_OUTCOME,
    ActionType.ASK_TOOL_RECOVERY_CONFIRMATION,
})


__all__ = [
    "AWAITED_SCORE", "BACKGROUND_SCORE", "MAX_FACTS", "MAX_SUMMARY",
    "REPORT_ACTIONS", "ReportLedger", "ReportState", "ToolOutcomeEvent",
    "ToolPayload", "build_outcome_event", "content_digest", "instruction_like",
    "planner_payload", "redact", "report_candidates",
]
