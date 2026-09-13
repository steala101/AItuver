"""Hard action eligibility for privacy-safe, non-verbal turn constraints."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from neuro_voice.cognition.types import ActionCandidate, ActionType


@dataclass(frozen=True, slots=True)
class ActionEligibility:
    """Frozen, text-free explanation of which actions this turn may take."""

    closure_signal: bool = False
    closure_confidence: float = 0.0
    direct_question: bool = False
    explicit_answer_request: bool = False
    new_task_instruction: bool = False
    safety_warning: bool = False
    response_obligation: str = "OPTIONAL"
    eligible_actions: tuple[ActionType, ...] = ()
    closure_response_policy: str = "adaptive"
    action_selection_reason: str = "no_action_constraint"

    @property
    def constrained(self) -> bool:
        return bool(self.eligible_actions)

    def snapshot(self) -> dict[str, Any]:
        return {
            "closure_signal": self.closure_signal,
            "closure_confidence": round(float(self.closure_confidence), 3),
            "direct_question": self.direct_question,
            "explicit_answer_request": self.explicit_answer_request,
            "new_task_instruction": self.new_task_instruction,
            "safety_warning": self.safety_warning,
            "response_obligation": self.response_obligation,
            "eligible_actions": [str(item) for item in self.eligible_actions],
            "closure_response_policy": self.closure_response_policy,
            "action_selection_reason": self.action_selection_reason,
        }

    @classmethod
    def from_turn(
        cls,
        *,
        closure_signal: bool,
        closure_confidence: float,
        closure_is_affirmative: bool,
        direct_question: bool,
        explicit_answer_request: bool,
        new_task_instruction: bool,
        safety_warning: bool,
        closure_response_policy: str,
    ) -> "ActionEligibility":
        """Build eligibility from existing intent detectors, never raw text."""
        base = {
            "closure_signal": bool(closure_signal),
            "closure_confidence": max(0.0, min(1.0, float(closure_confidence))),
            "direct_question": bool(direct_question),
            "explicit_answer_request": bool(explicit_answer_request),
            "new_task_instruction": bool(new_task_instruction),
            "safety_warning": bool(safety_warning),
            "closure_response_policy": str(closure_response_policy or "adaptive"),
        }
        if not closure_signal:
            reason = "closure_signal_low_confidence" if closure_confidence else "no_closure_signal"
            return cls(**base, action_selection_reason=reason)
        if direct_question:
            return cls(
                **base,
                response_obligation="REQUIRED",
                eligible_actions=(ActionType.ANSWER,),
                action_selection_reason="closure_with_direct_question",
            )
        if explicit_answer_request or closure_is_affirmative:
            return cls(
                **base,
                response_obligation="REQUIRED" if explicit_answer_request else "OPTIONAL",
                eligible_actions=(ActionType.BRIEF_ACKNOWLEDGE,),
                action_selection_reason=(
                    "closure_with_explicit_response_request"
                    if explicit_answer_request else "affirmative_closure"
                ),
            )
        if not new_task_instruction and not safety_warning:
            return cls(
                **base,
                eligible_actions=(ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE),
                action_selection_reason="closure_only",
            )
        return cls(**base, action_selection_reason="closure_with_higher_priority_intent")

    @classmethod
    def from_state(cls, state) -> "ActionEligibility":
        raw = dict(state.working_context.get("action_eligibility", {}) or {})
        names = raw.get("eligible_actions", ()) or ()
        actions: list[ActionType] = []
        for name in names:
            try:
                actions.append(ActionType(str(name)))
            except ValueError:
                continue
        return cls(
            closure_signal=bool(raw.get("closure_signal", False)),
            closure_confidence=float(raw.get("closure_confidence", 0.0) or 0.0),
            direct_question=bool(raw.get("direct_question", False)),
            explicit_answer_request=bool(raw.get("explicit_answer_request", False)),
            new_task_instruction=bool(raw.get("new_task_instruction", False)),
            safety_warning=bool(raw.get("safety_warning", False)),
            response_obligation=str(raw.get("response_obligation", "OPTIONAL") or "OPTIONAL"),
            eligible_actions=tuple(actions),
            closure_response_policy=str(raw.get("closure_response_policy", "adaptive") or "adaptive"),
            action_selection_reason=str(raw.get("action_selection_reason", "") or ""),
        )


@dataclass(frozen=True, slots=True)
class ActionConstraintResult:
    initially_selected_action: ActionType
    final_selected_action: ActionType
    action_constraint_result: str
    corrected_action: ActionType | None
    eligible_actions: tuple[ActionType, ...]

    def snapshot(self) -> dict[str, Any]:
        return {
            "initially_selected_action": str(self.initially_selected_action),
            "action_constraint_result": self.action_constraint_result,
            "corrected_action": str(self.corrected_action) if self.corrected_action else "",
            "final_selected_action": str(self.final_selected_action),
            "eligible_actions": [str(item) for item in self.eligible_actions],
        }


class ActionConstraintValidator:
    """Reject an ineligible selector result before planner/surface realization."""

    @staticmethod
    def validate(
        initially_selected: ActionCandidate,
        candidates: list[ActionCandidate],
        eligibility: ActionEligibility,
    ) -> tuple[ActionCandidate, ActionConstraintResult]:
        visible = tuple(eligibility.eligible_actions) or tuple(
            item.action_type for item in candidates
        )
        if not eligibility.constrained:
            return initially_selected, ActionConstraintResult(
                initially_selected.action_type, initially_selected.action_type,
                "NOT_APPLICABLE", None, visible,
            )
        allowed = [item for item in candidates if item.action_type in eligibility.eligible_actions]
        if initially_selected.action_type in eligibility.eligible_actions:
            return initially_selected, ActionConstraintResult(
                initially_selected.action_type, initially_selected.action_type,
                "VALID", None, visible,
            )
        if not allowed:
            # The kernel always supplies closure candidates. Keep the original
            # candidate only as a fail-safe; the caller records it as invalid.
            return initially_selected, ActionConstraintResult(
                initially_selected.action_type, initially_selected.action_type,
                "INVALID_ACTION_NO_ELIGIBLE_CANDIDATE", None, visible,
            )
        corrected = max(allowed, key=lambda item: item.total_score)
        return corrected, ActionConstraintResult(
            initially_selected.action_type, corrected.action_type,
            "INVALID_ACTION_CORRECTED", corrected.action_type, visible,
        )
