"""The control arm of the Planner A/B must not record a plan.

`ConversationPlanner` stores its decision in the profile as `last_plan`, and
that entry survives after the planner is switched off.  The first control run
therefore recorded eight turns of `proposal_path / reflective / long` —
identical values, none of them chosen by anything, left over from the last
turn the planner was enabled.  It even reported `target_length` violations
against a plan that did not exist.

A comparison in which the control side invents a plan measures nothing, so
this pins the behaviour: planner off means no shape, no style, no planned
length, and no violations.
"""
from __future__ import annotations

from types import SimpleNamespace

from neuro_voice.dialogue.intelligence import DialogueIntelligence
from neuro_voice.dialogue.plan_metrics import DiversityWindow

STALE_PLAN = {
    "response_shape": "proposal_path",
    "primary_style": "reflective",
    "target_length": "long",
    "ask_follow_up": False,
}


def _recorder(*, planner_enabled: bool):
    return SimpleNamespace(
        _cfg=SimpleNamespace(get=lambda key, default=None: default),
        _conversation_planner=SimpleNamespace(enabled=planner_enabled),
        _state={"turn_index": 1740},
        _diversity=DiversityWindow(8),
        last_plan_metrics=None,
    )


def _record(recorder, plan):
    DialogueIntelligence._record_plan_metrics(recorder, plan, "短い返事だよ。", {})
    return recorder.last_plan_metrics


def test_a_disabled_planner_records_no_plan():
    metrics = _record(_recorder(planner_enabled=False), dict(STALE_PLAN))
    assert metrics["planner"] is False
    assert metrics["shape"] == ""
    assert metrics["style"] == ""
    assert metrics["planned_length"] == ""
    assert metrics["planned_question"] is None
    assert metrics["violations"] == []


def test_an_enabled_planner_still_records_its_plan():
    metrics = _record(_recorder(planner_enabled=True), dict(STALE_PLAN))
    assert metrics["planner"] is True
    assert metrics["shape"] == "proposal_path"
    assert metrics["planned_length"] == "long"


def test_the_reply_itself_is_still_measured_without_a_planner():
    """Openings and lengths are what the control arm exists to compare."""
    metrics = _record(_recorder(planner_enabled=False), dict(STALE_PLAN))
    assert metrics["reply_chars"] > 0
    assert metrics["opening_hash"]
