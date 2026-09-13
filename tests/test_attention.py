"""継続的な注意。**常時LLMを呼ばずに「いま何を見ているか」を持つ。**

守りたいのは2つ。

1. **危険と直接質問は、何が積み上がっても押しのけられない**
2. **一瞬の変化で注目対象が毎フレーム入れ替わらない**

2を落とすと、話しかけている最中に別の話を始めるAIになる。実装としては
動いて見えるので、テストが無いと気づけない。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition.attention import (
    MAX_PENDING, AttentionEvent, AttentionEventType, ContinuousAttentionState,
    EventIntake, FocusKind, FocusManager, apply_event, note_initiative,
)


def event(kind, **kwargs) -> AttentionEvent:
    kwargs.setdefault("salience", .5)
    kwargs.setdefault("occurred_at", 1000.0)
    return AttentionEvent(event_type=kind, **kwargs)


def state_with(*events, focus=FocusKind.IDLE, since=1000.0, **kwargs) -> ContinuousAttentionState:
    return ContinuousAttentionState(
        active_focus=focus, focus_since=since,
        pending_opportunities=tuple(events), **kwargs)


# ---------------------------------------------------------------------------
# 間引き — 全フレームをイベントにしない
# ---------------------------------------------------------------------------


def test_the_same_change_arriving_ten_times_becomes_one_event():
    """**毎秒10回届く同じ変化を、10個の出来事として扱わない。**"""
    intake = EventIntake()
    accepted = sum(
        intake.accept(event(AttentionEventType.VISUAL_CHANGE,
                            deduplication_key="hp_bar"), now=1000.0 + i * .1)
        for i in range(10)
    )
    assert accepted == 1, accepted
    assert intake.dropped["duplicate"] == 9


def test_the_same_change_is_accepted_again_after_the_window():
    intake = EventIntake(dedup_window=4.0)
    assert intake.accept(event(AttentionEventType.GAME_EVENT, deduplication_key="k"), now=1000.0)
    assert not intake.accept(event(AttentionEventType.GAME_EVENT, deduplication_key="k"), now=1002.0)
    assert intake.accept(event(AttentionEventType.GAME_EVENT, deduplication_key="k"), now=1005.0)


def test_a_trivial_change_never_becomes_an_event():
    intake = EventIntake()
    assert not intake.accept(event(AttentionEventType.VISUAL_CHANGE, salience=.05))
    assert intake.dropped["below_salience"] == 1


def test_danger_is_never_dropped_for_being_quiet():
    """**取りこぼす方が高くつく。** 危険と直接質問は重要度で落とさない。"""
    intake = EventIntake()
    for kind in (AttentionEventType.DANGER_EVENT, AttentionEventType.DIRECT_QUESTION):
        assert intake.accept(event(kind, salience=.01, deduplication_key="x")), kind


def test_danger_is_not_deduplicated_away():
    """同じ危険が続いているなら、それは黙る理由にならない。"""
    intake = EventIntake()
    for index in range(3):
        assert intake.accept(
            event(AttentionEventType.DANGER_EVENT, deduplication_key="lava"),
            now=1000.0 + index * .1), index


def test_an_expired_event_is_refused():
    intake = EventIntake()
    stale = AttentionEvent(
        event_type=AttentionEventType.GAME_EVENT, occurred_at=1000.0, expires_at=1001.0)
    assert not intake.accept(stale, now=1002.0)
    assert intake.dropped["expired"] == 1


def test_an_uncertain_event_is_refused():
    intake = EventIntake()
    assert not intake.accept(event(AttentionEventType.VISUAL_CHANGE, confidence=.1))


# ---------------------------------------------------------------------------
# 注意状態 — 短い構造だけ
# ---------------------------------------------------------------------------


def test_the_state_keeps_no_thinking_text():
    """**内部思考文も Chain of Thought も保存しない。**"""
    import json

    state = ContinuousAttentionState()
    apply_event(state, event(
        AttentionEventType.USER_SPEECH_ENDED,
        summary="口座番号は1234だと言っていた", topic_ids=("設計",)), now=1000.0)
    text = json.dumps(state.summary(), ensure_ascii=False)
    assert "1234" not in text and "口座番号" not in text
    assert state.summary()["topics"] == ["設計"]


def test_pending_events_do_not_grow_without_limit():
    state = ContinuousAttentionState()
    for index in range(MAX_PENDING + 8):
        apply_event(state, event(AttentionEventType.GAME_EVENT,
                                 deduplication_key=f"k{index}"), now=1000.0)
    assert len(state.pending_opportunities) == MAX_PENDING


def test_expired_events_leave_the_queue():
    state = ContinuousAttentionState()
    apply_event(state, AttentionEvent(
        event_type=AttentionEventType.GAME_EVENT, occurred_at=1000.0,
        expires_at=1001.0), now=1000.0)
    apply_event(state, event(AttentionEventType.USER_SPEECH_ENDED), now=1005.0)
    assert len(state.pending_opportunities) == 1


def test_user_speech_clears_the_silence_counter():
    state = ContinuousAttentionState(silence_duration=45.0)
    apply_event(state, event(AttentionEventType.USER_SPEECH_STARTED), now=1000.0)
    assert state.silence_duration == 0.0
    assert state.user_activity_state == "speaking"


def test_applying_an_event_does_not_move_the_focus():
    """**取り込みと選択を混ぜない。** 混ぜるとヒステリシスが効かなくなる。"""
    state = ContinuousAttentionState(active_focus=FocusKind.GAMEPLAY)
    apply_event(state, event(AttentionEventType.USER_SPEECH_STARTED), now=1000.0)
    assert str(state.active_focus) == str(FocusKind.GAMEPLAY)


def test_initiatives_are_counted_within_a_window():
    state = ContinuousAttentionState()
    note_initiative(state, "comment", now=1000.0)
    note_initiative(state, "comment", now=1030.0)
    note_initiative(state, "comment", now=1200.0)
    assert state.initiatives_within(60.0, now=1040.0) == 2


# ---------------------------------------------------------------------------
# 優先順位 — 段が先、点数は後
# ---------------------------------------------------------------------------


def test_danger_beats_everything_no_matter_the_score():
    """**危険は「たまたま点が高かった」で選ばれる形にしない。**"""
    state = state_with(
        event(AttentionEventType.GAME_EVENT, salience=1.0, novelty=1.0, urgency=1.0),
        event(AttentionEventType.DANGER_EVENT, salience=.1, novelty=.0, urgency=.1),
        focus=FocusKind.GAMEPLAY, since=1.0,
    )
    decision = FocusManager().select(state, now=1000.0)
    assert str(decision.focus) == str(FocusKind.DANGER)
    assert decision.switched


def test_a_direct_question_beats_a_game_event():
    state = state_with(
        event(AttentionEventType.GAME_EVENT, salience=1.0, urgency=.9),
        event(AttentionEventType.DIRECT_QUESTION, salience=.4),
        focus=FocusKind.GAMEPLAY, since=1.0,
    )
    assert str(FocusManager().select(state, now=1000.0).focus) == str(FocusKind.USER_SPEECH)


@pytest.mark.parametrize("higher,lower", [
    (AttentionEventType.DANGER_EVENT, AttentionEventType.DIRECT_QUESTION),
    (AttentionEventType.DIRECT_QUESTION, AttentionEventType.USER_SPEECH_ENDED),
    (AttentionEventType.USER_SPEECH_ENDED, AttentionEventType.OBLIGATION_DUE),
    (AttentionEventType.OBLIGATION_DUE, AttentionEventType.GAME_EVENT),
    (AttentionEventType.GAME_EVENT, AttentionEventType.MEMORY_RELEVANCE),
    (AttentionEventType.MEMORY_RELEVANCE, AttentionEventType.VISUAL_CHANGE),
])
def test_the_priority_order_holds(higher, lower):
    """仕様の順序をそのまま固定する。並べ替えたら気づけるように。"""
    assert event(higher).tier < event(lower).tier, (higher, lower)


def test_an_environment_change_does_not_outrank_a_person():
    state = state_with(
        event(AttentionEventType.VISUAL_CHANGE, salience=1.0, novelty=1.0),
        event(AttentionEventType.USER_SPEECH_ENDED, salience=.2),
        focus=FocusKind.IDLE, since=1.0,
    )
    assert str(FocusManager().select(state, now=1000.0).focus) == str(FocusKind.USER_SPEECH)


# ---------------------------------------------------------------------------
# ヒステリシス — 毎フレーム切り替わらない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("challenger", [.55, .65, .75, .85])
def test_a_slightly_better_option_does_not_steal_the_focus(challenger):
    """**差が僅かなら現状維持。** ここが無いと注目が振動する。

    現状維持の下駄で押し切る場合と、僅差として退ける場合の両方があるが、
    外から見れば同じ——**乗り換えない**。理由の文言ではなく、そこを固定する。
    """
    state = state_with(
        event(AttentionEventType.GAME_EVENT, salience=.50, deduplication_key="a"),
        event(AttentionEventType.TASK_PROGRESS, salience=challenger, deduplication_key="b"),
        focus=FocusKind.GAMEPLAY, since=1.0,
    )
    decision = FocusManager().select(state, now=1000.0)
    assert not decision.switched, (challenger, decision.snapshot())
    assert str(decision.focus) == str(FocusKind.GAMEPLAY)


def test_the_margin_branch_is_reachable():
    """僅差で退ける経路が本当に通ること。下駄だけで守っていたら意味が薄い。"""
    manager = FocusManager()
    for challenger in (x / 100 for x in range(50, 100)):
        state = state_with(
            event(AttentionEventType.GAME_EVENT, salience=.50, deduplication_key="a"),
            event(AttentionEventType.TASK_PROGRESS, salience=challenger,
                  deduplication_key="b"),
            focus=FocusKind.GAMEPLAY, since=1.0,
        )
        if "margin" in manager.select(state, now=1000.0).reason:
            return
    pytest.fail("僅差で退ける経路に一度も入らなかった")


def test_a_clearly_better_option_does_take_the_focus():
    state = state_with(
        event(AttentionEventType.GAME_EVENT, salience=.1, novelty=.0, deduplication_key="a"),
        event(AttentionEventType.TASK_PROGRESS, salience=1.0, novelty=1.0,
              urgency=.9, deduplication_key="b"),
        focus=FocusKind.GAMEPLAY, since=1.0,
    )
    decision = FocusManager().select(state, now=1000.0)
    assert decision.switched, decision.snapshot()
    assert str(decision.focus) == str(FocusKind.SHARED_TASK)


def test_the_focus_cannot_change_twice_in_the_same_moment():
    """**乗り換えた直後は動かさない。** 一瞬の変化で振動しないため。"""
    state = state_with(
        event(AttentionEventType.GAME_EVENT, salience=.2, deduplication_key="a"),
        event(AttentionEventType.TASK_PROGRESS, salience=1.0, urgency=.9,
              deduplication_key="b"),
        focus=FocusKind.GAMEPLAY, since=999.8,
    )
    decision = FocusManager().select(state, now=1000.0)
    assert not decision.switched
    assert "min_dwell" in decision.reason


def test_the_dwell_lock_never_holds_back_danger():
    """**滞在時間の制限が危険を遅らせてはいけない。**"""
    state = state_with(
        event(AttentionEventType.DANGER_EVENT, salience=.3, deduplication_key="d"),
        focus=FocusKind.GAMEPLAY, since=999.99,
    )
    decision = FocusManager().select(state, now=1000.0)
    assert decision.switched
    assert "higher_tier" in decision.reason


def test_hysteresis_is_one_mechanism_not_two():
    """**同じ目的の仕組みを2つ置かない。**

    最初は「現状維持へ加点」と「乗り換えに差を要求」の両方を入れていたが、
    加点だけで全部決まり、差の判定は一度も通っていなかった。
    片方が死んでいることに気づけない形は残さない。
    """
    manager = FocusManager()
    assert not hasattr(manager, "incumbent_bonus")
    state = state_with(
        event(AttentionEventType.GAME_EVENT, salience=.5, deduplication_key="a"),
        focus=FocusKind.GAMEPLAY, since=1.0)
    assert "incumbent_bonus" not in manager.select(state, now=1000.0).components


def test_committing_resets_the_dwell_clock():
    manager = FocusManager()
    state = state_with(
        event(AttentionEventType.DANGER_EVENT, deduplication_key="d"),
        focus=FocusKind.GAMEPLAY, since=1.0)
    manager.commit(state, manager.select(state, now=1000.0), now=1000.0)
    assert str(state.active_focus) == str(FocusKind.DANGER)
    assert state.focus_since == 1000.0
    assert str(FocusKind.GAMEPLAY) in [str(x) for x in state.secondary_focuses]


def test_staying_does_not_reset_the_dwell_clock():
    manager = FocusManager()
    state = state_with(
        event(AttentionEventType.GAME_EVENT, deduplication_key="a"),
        focus=FocusKind.GAMEPLAY, since=500.0)
    manager.commit(state, manager.select(state, now=1000.0), now=1000.0)
    assert state.focus_since == 500.0


# ---------------------------------------------------------------------------
# 点数の中身
# ---------------------------------------------------------------------------


def test_an_old_event_loses_relevance():
    manager = FocusManager()
    state = ContinuousAttentionState()
    fresh, _ = manager.score(event(AttentionEventType.GAME_EVENT, occurred_at=1000.0),
                             state, now=1000.0)
    old, _ = manager.score(event(AttentionEventType.GAME_EVENT, occurred_at=900.0),
                           state, now=1000.0)
    assert old < fresh


def test_speaking_makes_it_costly_to_look_away():
    """言い切る前に別のものへ向かない。**ただし危険は別。**"""
    manager = FocusManager()
    speaking = ContinuousAttentionState(assistant_activity_state="speaking")
    quiet = ContinuousAttentionState(assistant_activity_state="idle")
    game = event(AttentionEventType.GAME_EVENT)
    assert manager.score(game, speaking, now=1000.0)[0] < manager.score(game, quiet, now=1000.0)[0]
    danger = event(AttentionEventType.DANGER_EVENT)
    assert manager.score(danger, speaking, now=1000.0)[1]["interruption_cost"] == 0.0


def test_a_matching_topic_raises_relevance():
    manager = FocusManager()
    state = ContinuousAttentionState(current_topic_ids=("配線",))
    on_topic, _ = manager.score(
        event(AttentionEventType.GAME_EVENT, topic_ids=("配線",)), state, now=1000.0)
    off_topic, _ = manager.score(
        event(AttentionEventType.GAME_EVENT, topic_ids=("天気",)), state, now=1000.0)
    assert on_topic > off_topic


def test_no_live_events_keeps_the_current_focus():
    state = ContinuousAttentionState(active_focus=FocusKind.GAMEPLAY)
    decision = FocusManager().select(state, now=1000.0)
    assert str(decision.focus) == str(FocusKind.GAMEPLAY)
    assert decision.reason == "no_live_event"
    assert not decision.switched


def test_the_decision_is_readable_afterwards():
    """なぜその向き先になったかを後から読めること（第20条）。"""
    state = state_with(
        event(AttentionEventType.DANGER_EVENT, deduplication_key="d"),
        event(AttentionEventType.GAME_EVENT, deduplication_key="g"),
        focus=FocusKind.IDLE, since=1.0)
    payload = FocusManager().select(state, now=1000.0).snapshot()
    assert payload["focus"] == "danger"
    assert payload["reason"] and payload["components"]
    assert payload["runners_up"]


def test_selecting_does_not_change_the_state():
    """**決定と適用を分ける。** 決めた理由だけ残して適用しない使い方のため。"""
    state = state_with(
        event(AttentionEventType.DANGER_EVENT, deduplication_key="d"),
        focus=FocusKind.IDLE, since=1.0)
    before = (str(state.active_focus), state.focus_since, state.version)
    FocusManager().select(state, now=1000.0)
    assert (str(state.active_focus), state.focus_since, state.version) == before
