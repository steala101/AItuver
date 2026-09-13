"""決定と、それを実行する経路の間の境界。**既定は「話さない」。**

これまでは発話経路がまず走り、沈黙は空文字で表現されていた。それだと

* 沈黙と失敗が区別できない
* fallback の再生成が沈黙を上書きする
* 「何も答えないで」とLLMへ頼むことになる（文章は生成される）

`REMAIN_SILENT` は空文字を作ることではなく、**文章生成経路そのものを
呼ばないこと**である。ここがその分岐を1箇所で決める。

大きな状態機械は作らない。責務は「どの経路を通すか決めて、通らなかった
場合の結果を作る」だけ。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from neuro_voice.cognition.types import (
    ActionDecision, ActionOutcome, ActionType, ExecutionStatus, SpeechPolicy,
)

#: 発話しない行動のうち、内部処理を伴うもの。沈黙とは結果を分ける。
_INTERNAL_ACTIONS = frozenset({
    ActionType.STORE_MEMORY, ActionType.RETRIEVE_MEMORY,
    ActionType.ABANDON_INTERRUPTED_RESPONSE,
    # 道具の実行と「待つ」は、黙っているが**何もしなかった訳ではない**。
    # 沈黙と同じ結果にすると、実行したのに記録が残らない。
    ActionType.EXECUTE_TOOL, ActionType.WAIT,
})


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """この決定で、どの経路を通すか。"""

    route: str                      # speech / silent / internal
    speech_allowed: bool
    #: 中断されたまま残っている発話を捨てるか。
    clear_pending_response: bool = False
    #: 現在の話題を閉じるか。**セッションは閉じない。**
    close_topic: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def planner_allowed(self) -> bool:
        return self.speech_allowed

    def snapshot(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "speech_allowed": self.speech_allowed,
            "clear_pending_response": self.clear_pending_response,
            "close_topic": self.close_topic,
            "warnings": list(self.warnings),
        }


class ActionExecutionGate:
    """`ActionDecision` を実行契約として扱う。"""

    @staticmethod
    def plan(decision: ActionDecision, *, end_signal: float = 0.0) -> ExecutionPlan:
        warnings: list[str] = []
        if decision.contradictory:
            # 沈黙と発話が同時に指定されている。安全側へ倒し、痕跡を残す。
            warnings.append("contradictory_decision:speech_forbidden")
        policy = decision.speech_policy
        action = decision.selected_action

        if policy is not SpeechPolicy.FORBIDDEN:
            return ExecutionPlan(
                "speech", True,
                # 再開しないなら、残っている発話は持ち越さない。
                clear_pending_response=action is not ActionType.RESUME_INTERRUPTED_RESPONSE,
                warnings=tuple(warnings),
            )
        if action in _INTERNAL_ACTIONS:
            return ExecutionPlan(
                "internal", False,
                clear_pending_response=action is ActionType.ABANDON_INTERRUPTED_RESPONSE,
                close_topic=action is ActionType.ABANDON_INTERRUPTED_RESPONSE,
                warnings=tuple(warnings),
            )
        # REMAIN_SILENT。**セッションは閉じない。話題も既定では閉じない。**
        # 打ち切りの合図が強い時だけ、この話題を休止する。
        ending = end_signal > .5
        return ExecutionPlan(
            "silent", False,
            clear_pending_response=ending,
            close_topic=ending,
            warnings=tuple(warnings),
        )

    @staticmethod
    def non_speech_outcome(
        decision: ActionDecision, plan: ExecutionPlan, *,
        started_at: float | None = None,
        affected_obligation_ids: tuple[str, ...] = (),
    ) -> ActionOutcome:
        """発話しなかったターンの結果。**失敗ではない。**"""
        return ActionOutcome(
            action_id=decision.action_id,
            turn_id=decision.turn_id,
            status=(
                ExecutionStatus.INTERNAL_COMPLETED if plan.route == "internal"
                else ExecutionStatus.SILENT_COMPLETED
            ),
            started_at=started_at if started_at is not None else time.time(),
            completed_at=time.time(),
            speech_generated=False,
            planner_called=False,
            realizer_called=False,
            tts_called=False,
            pending_response_cleared=plan.clear_pending_response,
            turn_closed=True,
            topic_closed=plan.close_topic,
            affected_obligation_ids=affected_obligation_ids,
        )

    @staticmethod
    def silence_contract(decision: ActionDecision | None, plan: ExecutionPlan) -> dict[str, str | bool]:
        """Validate the provenance required before a turn can end silently."""
        action = str(getattr(decision, "selected_action", "") or "")
        decision_id = str(getattr(decision, "action_id", "") or "")
        reason = str(getattr(decision, "decision_reason", "") or "")
        valid = bool(
            plan.route == "silent"
            and action == str(ActionType.REMAIN_SILENT)
            and decision_id
            and reason
        )
        return {
            "valid": valid,
            "action_decision_id": decision_id,
            "decision_source": "ACTION_SELECTOR" if decision_id else "",
            "silence_reason_code": reason,
            "suppression_reason": "action_selector_remain_silent" if valid else "",
        }


def obligations_to_release(
    obligations: tuple[str, ...] | list[str], plan: ExecutionPlan,
) -> tuple[str, ...]:
    """取り消してよい義務だけを選ぶ。**一括削除しない。**

    中断発話を捨てると決めた時に消えるのは「言い終える」義務であって、
    それ以外の保留（相手への質問、次回に回した話題）は残る。
    """
    if not plan.clear_pending_response:
        return ()
    return tuple(
        item for item in obligations
        if "interrupted" in str(item) or "finish" in str(item)
    )
