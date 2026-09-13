"""経験を覚え、必要な時に思い出し、行動を変える。

Phase 3 の必須シナリオ10件。**「保存できる」ではなく「保存しない方が多い」**
ことと、**思い出した記憶が実際に行動選択を変える**ことを固定する。

プロンプトへ足すだけの実装だと、ここのテストは全部通らない——
記憶が候補の点数を動かしていないので。
"""
from __future__ import annotations

import time

import pytest

from neuro_voice.cognition import (
    ActionType, CognitiveEvent, CognitiveKernel, EventType, EpisodicMemory,
    InformationType, MemoryCandidate, MemoryStatus, MemoryWriteGate,
    Reflection, WriteDecision, build_query, build_state,
    derive_conversation_strategy, influence_from, planner_payload,
    propose_candidates, rank, retrieval_trigger, revise, self_failure_candidate,
    should_reflect,
)
from neuro_voice.cognition.episodic import contradicts, stance
from neuro_voice.cognition.reflection import ReflectionStatus


def gate() -> MemoryWriteGate:
    return MemoryWriteGate()


def memory(summary: str, **kwargs) -> EpisodicMemory:
    kwargs.setdefault("memory_id", abs(hash(summary)) % 10000 or 1)
    kwargs.setdefault("occurred_at", time.time())
    return EpisodicMemory(summary=summary, **kwargs)


# ---------------------------------------------------------------------------
# ケース1: 重要でない雑談は残さない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "おはよう", "こんばんは", "うん", "そっか", "なるほどね", "はい",
])
def test_case1_small_talk_is_not_stored(text):
    """**全ターンを長期保存しない。** ここが落ちると記憶がゴミで埋まる。"""
    assert propose_candidates(text, "おはよう！") == []


def test_case1_the_gate_also_refuses_a_trivial_candidate():
    """候補づくりを迂回してもゲートで落ちる。**入口を2つにしない。**"""
    verdict = gate().evaluate(MemoryCandidate(proposed_summary="うん", importance=.9))
    assert verdict.decision is WriteDecision.SKIP
    assert "trivial_turn" in verdict.reasons


def test_case1_an_uncertain_transcript_is_not_stored():
    """一時的なASR誤認識を覚えない。"""
    assert propose_candidates("単調な一問一答は嫌い", input_confidence=.3) == []


# ---------------------------------------------------------------------------
# ケース2: 明示的な好みは高い確信で残る
# ---------------------------------------------------------------------------


def test_case2_an_explicit_preference_is_stored_as_a_statement():
    candidates = propose_candidates("単調な一問一答は嫌い")
    assert candidates, "明示的な好みが候補にならなかった"
    candidate = candidates[0]
    assert candidate.information_type == InformationType.USER_STATEMENT
    assert candidate.confidence >= .9
    verdict = gate().evaluate(candidate)
    assert verdict.decision is WriteDecision.STORE


def test_case2_it_reaches_the_planner_in_a_short_structured_form():
    """**生の会話履歴を投げない。** 出典と確信度つきの短い構造だけ。"""
    stored = memory(
        "単調な一問一答は嫌い", information_type=InformationType.USER_STATEMENT,
        event_type="preference", confidence=.98, importance=.8,
    )
    scored = rank([stored], build_query("前に言った一問一答の話", trigger="past_reference"))
    payload = planner_payload(scored)
    assert payload and payload[0]["type"] == "USER_STATEMENT"
    assert payload[0]["confidence"] == .98
    assert payload[0]["relevance"] == "response_style"
    assert set(payload[0]) == {"memory_id", "type", "summary", "confidence", "relevance"}


# ---------------------------------------------------------------------------
# ケース3: 推論と事実を分ける
# ---------------------------------------------------------------------------


def test_case3_a_low_confidence_inference_is_held_not_asserted():
    """**推論を事実として置かない。** 仮説のまま低優先で持つ。"""
    verdict = gate().evaluate(MemoryCandidate(
        proposed_summary="ユーザーは短い回答を好む可能性がある",
        information_type=InformationType.INFERENCE,
        importance=.7, expected_future_utility=.8, confidence=.45,
    ))
    assert verdict.decision is WriteDecision.HOLD
    assert str(verdict.stored_status) == str(MemoryStatus.LOW_PRIORITY)


def test_case3_one_episode_does_not_make_a_reflection():
    """1件で人格判断をしない。"""
    single = [memory("説明を続けた", event_type="self_failure:kept_talking_after_end_signal")]
    assert derive_conversation_strategy(single) is None


def test_case3_several_episodes_make_a_low_confidence_hypothesis():
    episodes = [
        memory(f"説明を続けた{i}", memory_id=i + 1,
               event_type="self_failure:kept_talking_after_end_signal")
        for i in range(3)
    ]
    reflection = derive_conversation_strategy(episodes)
    assert reflection is not None
    assert .25 <= reflection.confidence <= .75, reflection.confidence
    assert reflection.source_memory_ids == (1, 2, 3)


def test_case3_a_reflection_describes_behaviour_not_personality():
    """**レッテルを貼らない。** 「怒りっぽい」ではなく観測された傾向。"""
    episodes = [
        memory(f"x{i}", memory_id=i + 1,
               event_type="self_failure:repeated_the_same_explanation")
        for i in range(3)
    ]
    reflection = derive_conversation_strategy(episodes)
    assert reflection is not None
    for label in ("怒りっぽい", "せっかち", "気難しい", "な人"):
        assert label not in reflection.statement
    assert "観測された" in reflection.statement


# ---------------------------------------------------------------------------
# ケース4: 訂正
# ---------------------------------------------------------------------------


def test_case4_stance_reads_which_side_is_preferred():
    """文字列の似方では絶対に取れない。**軸と向き**で見る。"""
    assert stance("ユーザーは短い回答を好む") == ("response_length", 1)
    assert stance("短ければいいわけじゃない。内容は詳しい方がいい")[1] == -1


def test_case4_an_explicit_statement_supersedes_an_inference():
    old = memory(
        "ユーザーは短い回答を好む", memory_id=7,
        information_type=InformationType.INFERENCE, confidence=.45,
    )
    verdict = gate().evaluate(MemoryCandidate(
        proposed_summary="短ければいいわけじゃない。内容は詳しい方がいい",
        information_type=InformationType.USER_STATEMENT,
        importance=.85, confidence=.95,
    ), [old])
    assert verdict.decision is WriteDecision.SUPERSEDE
    assert verdict.target_memory_id == 7


def test_case4_the_old_memory_is_not_deleted():
    """**訂正履歴を追跡可能にする。** 消すと間違った訂正に気づけない。"""
    verdict = gate().evaluate(MemoryCandidate(
        proposed_summary="短ければいいわけじゃない。詳しい方がいい",
        information_type=InformationType.USER_STATEMENT, confidence=.95, importance=.85,
    ), [memory("短い回答を好む", memory_id=7,
               information_type=InformationType.INFERENCE)])
    # 判断は「古い方に印を付けて、新しい方から繋ぐ」。削除は含まれない。
    assert verdict.decision is not WriteDecision.SKIP
    assert verdict.target_memory_id == 7
    assert verdict.writes, "新しい記憶が作られないと繋ぎ先が無い"


def test_case4_a_superseded_memory_is_not_retrieved():
    old = memory("短い回答を好む", memory_id=7, status=MemoryStatus.SUPERSEDED,
                 importance=.9, confidence=.9)
    new = memory("詳しい説明の方がいい", memory_id=8,
                 information_type=InformationType.USER_STATEMENT,
                 event_type="correction", importance=.9, confidence=.95)
    scored = rank([old, new], build_query("説明の長さの話", trigger="past_reference"))
    assert [item.memory.memory_id for item in scored] == [8]


# ---------------------------------------------------------------------------
# ケース5: 過去の失敗が行動を変える
# ---------------------------------------------------------------------------


def test_case5_a_past_failure_penalises_the_same_action():
    """**ここが Phase 3 の核心。** 記憶が候補の点数を動かす。"""
    failures = [
        memory(f"x{i}", memory_id=i + 1, importance=.8, confidence=.9,
               event_type="self_failure:kept_talking_after_end_signal")
        for i in range(2)
    ]
    query = build_query("もういいよ", trigger="similar_to_past_failure",
                        failure_patterns=("kept_talking_after_end_signal",))
    influence = influence_from(rank(failures, query))
    assert influence.for_action(ActionType.CONTINUE_PREVIOUS_TOPIC) < 0
    assert influence.sources(ActionType.CONTINUE_PREVIOUS_TOPIC)


def test_case5_the_selected_action_actually_changes():
    state = build_state(
        user_state={"end_signal": .9, "engagement": .05},
        obligations=("前の説明の続き",),
    )
    event = CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="もういいよ")

    without = CognitiveKernel().decide(event, state)
    influence = influence_from(rank(
        [memory(f"x{i}", memory_id=i + 1, importance=.8, confidence=.9,
                event_type="self_failure:kept_talking_after_end_signal")
         for i in range(3)],
        build_query("もういいよ", trigger="similar_to_past_failure",
                    failure_patterns=("kept_talking_after_end_signal",)),
    ))
    with_memory = CognitiveKernel(memory_influence=influence)
    candidates = with_memory.propose(event, state)
    scores = {str(c.action_type): c.total_score for c in candidates}
    baseline = {str(c.action_type): c.total_score
                for c in CognitiveKernel().propose(event, state)}
    assert scores["continue_previous_topic"] < baseline["continue_previous_topic"]
    assert with_memory.decide(event, state, candidates).selected_action in {
        ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE,
    }
    assert str(without.selected_action)  # 比較対象があることの確認


def test_case5_the_trace_can_tell_which_memory_moved_which_score():
    """どの記憶がどのスコアへ効いたかを追える（第20条）。"""
    influence = influence_from(rank(
        [memory("x", memory_id=42, importance=.8, confidence=.9,
                event_type="self_failure:asked_a_question_already_answered")],
        build_query("さっきから同じこと聞くね", trigger="relationship_event",
                    failure_patterns=("asked_a_question_already_answered",)),
    ))
    candidates = CognitiveKernel(memory_influence=influence).propose(
        CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="どう思う"),
        build_state(working_memory={"ambiguity": .9}),
    )
    clarify = [c for c in candidates if c.action_type is ActionType.ASK_CLARIFICATION]
    assert clarify, "聞き返しが候補に出ていない"
    assert "memory_adjustment" in clarify[0].score_components
    assert "memory:42" in clarify[0].reasons


# ---------------------------------------------------------------------------
# ケース6: 未完了の約束
# ---------------------------------------------------------------------------


def test_case6_a_promise_becomes_a_candidate():
    candidates = propose_candidates("後で設定を確認しておいて")
    assert any(c.event_type == "promise" for c in candidates), candidates


def test_case6_an_open_obligation_triggers_retrieval_and_adds_score():
    state = build_state(obligations=("設定の確認",))
    assert retrieval_trigger("設定どうなった", state) == "unresolved_obligation"
    promise = memory("後で設定を確認しておく", memory_id=3, event_type="promise",
                     importance=.85, confidence=.9, goal_relevance=.8)
    scored = rank([promise], build_query("設定どうなった", state, trigger="unresolved_obligation"))
    assert scored, "未完了の約束が引けていない"
    assert scored[0].relevance == "unfinished_obligation"
    assert influence_from(scored).for_action(ActionType.CONTINUE_PREVIOUS_TOPIC) > 0


# ---------------------------------------------------------------------------
# ケース7: 重複
# ---------------------------------------------------------------------------


def test_case7_saying_the_same_thing_again_reinforces_instead_of_duplicating():
    existing = memory("単調な一問一答は嫌い",
                      information_type=InformationType.USER_STATEMENT,
                      event_type="preference", confidence=.9)
    verdict = gate().evaluate(MemoryCandidate(
        proposed_summary="単調な一問一答は嫌いなんだよね",
        information_type=InformationType.USER_STATEMENT,
        importance=.8, confidence=.95,
    ), [existing])
    assert verdict.decision is WriteDecision.REINFORCE
    assert not verdict.writes, "同じ内容で行を増やしてはいけない"
    assert verdict.confidence > existing.confidence


def test_case7_an_inference_is_upgraded_when_the_user_says_it():
    existing = memory("一問一答は好まないかもしれない",
                      information_type=InformationType.INFERENCE, confidence=.5)
    verdict = gate().evaluate(MemoryCandidate(
        proposed_summary="一問一答は好まないかもしれないって話、そのとおり",
        information_type=InformationType.USER_STATEMENT,
        importance=.8, confidence=.95,
    ), [existing])
    assert verdict.decision is WriteDecision.UPDATE


# ---------------------------------------------------------------------------
# ケース8: LLMを交換しても記憶が残る
# ---------------------------------------------------------------------------


def test_case8_memory_survives_swapping_the_llm(tmp_path):
    """**LLMは記憶の持ち主ではない**（第5条）。"""
    from neuro_voice.mind.episodes import EpisodicMemoryService
    from neuro_voice.mind.store import MemoryStore

    class Cfg:
        def get(self, key, default=None):
            return {"memory.episodic_enabled": True,
                    "memory.retrieval_enabled": True}.get(key, default)

    store = MemoryStore(tmp_path / "mind.db")
    service = EpisodicMemoryService(store, Cfg())
    service.observe_turn("単調な一問一答は嫌い", "わかった")
    store.close()

    # LLMを差し替えた = 別インスタンスでDBを開き直した、と同じこと。
    reopened = MemoryStore(tmp_path / "mind.db")
    rows = reopened.episodes()
    assert any("一問一答" in row["text"] for row in rows), rows
    assert rows[0]["information_type"] == "user_statement"
    reopened.close()


def test_case8_an_old_database_still_opens(tmp_path):
    """**既存のDBを壊さない。** 列を足すだけの加算的マイグレーション。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " kind TEXT NOT NULL DEFAULT 'fact', text TEXT NOT NULL,"
        " importance INTEGER NOT NULL DEFAULT 3, embedding BLOB, dim INTEGER,"
        " source TEXT DEFAULT '', created_at REAL NOT NULL, last_accessed REAL,"
        " access_count INTEGER NOT NULL DEFAULT 0);"
    )
    conn.execute(
        "INSERT INTO memories (kind, text, importance, created_at)"
        " VALUES ('fact','猫を飼っている',4,1.0)")
    conn.commit()
    conn.close()

    from neuro_voice.mind.store import MemoryStore

    store = MemoryStore(path)
    rows = store.episodes()
    assert any("猫" in row["text"] for row in rows), "既存の記憶が読めなくなった"
    assert rows[0]["status"] == "active"
    store.close()


# ---------------------------------------------------------------------------
# ケース9: 検索予算
# ---------------------------------------------------------------------------


def test_case9_a_greeting_does_not_search_long_term_memory():
    """**毎ターン全記憶を取得しない。**"""
    assert retrieval_trigger("おはよう", build_state()) == ""
    assert retrieval_trigger("うん", build_state()) == ""


def test_case9_only_a_few_memories_come_back_from_many():
    many = [
        memory(f"設計の話 その{i}", memory_id=i + 1, importance=.5, confidence=.7)
        for i in range(300)
    ]
    scored = rank(many, build_query("前に話した設計の話", trigger="past_reference"), limit=3)
    assert len(scored) == 3, len(scored)
    assert len(planner_payload(scored)) == 3


def test_case9_ranking_many_memories_is_fast():
    """検索がターンの待ち時間を目に見えて伸ばさないこと。"""
    many = [memory(f"話題{i}", memory_id=i + 1) for i in range(500)]
    query = build_query("前に話した話題", trigger="past_reference")
    started = time.perf_counter()
    rank(many, query, limit=3)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 200, f"{elapsed_ms:.1f}ms"


# ---------------------------------------------------------------------------
# ケース10: 反証
# ---------------------------------------------------------------------------


def test_case10_an_explicit_statement_weakens_a_contradicting_reflection():
    """**本人の発言は仮説より常に強い。**"""
    reflection = Reflection(
        statement="冗談を挟むと反応が悪くなる場面が複数回観測された",
        confidence=.6, support_count=4,
    )
    revise(reflection, "軽い冗談は好きだよ")
    assert reflection.confidence < .6
    assert reflection.contradiction_count == 1


def test_case10_a_weakened_reflection_stops_being_used_but_is_kept():
    reflection = Reflection(statement="冗談を挟むと反応が悪くなる傾向", confidence=.3)
    revise(reflection, "軽い冗談は好き")
    assert not reflection.usable
    assert str(reflection.status) == str(ReflectionStatus.RETIRED)
    assert reflection.statement, "**消さない。** 外した判断も記録として残す"


def test_case10_an_unrelated_statement_does_not_move_confidence():
    reflection = Reflection(statement="冗談を挟むと反応が悪くなる傾向", confidence=.6)
    revise(reflection, "今日は雨だね")
    assert reflection.confidence == .6


def test_contradiction_needs_the_same_axis():
    """違う話題を矛盾と決めつけない。"""
    assert contradicts("短い方がいい", "詳しい方がいい")
    assert not contradicts("短い方がいい", "冗談は好き")
    assert not contradicts("よく分からない話", "詳しい方がいい")


# ---------------------------------------------------------------------------
# 実行条件・安全側
# ---------------------------------------------------------------------------


def test_reflection_does_not_run_every_turn():
    assert should_reflect(important_episodes=1) == ""
    assert should_reflect(important_episodes=8) == "episode_threshold"
    assert should_reflect(repeated_failures=3) == "repeated_failures"


def test_credentials_never_reach_long_term_memory():
    verdict = gate().evaluate(MemoryCandidate(
        proposed_summary="認証コードは1234",
        information_type=InformationType.USER_STATEMENT,
        importance=1.0, confidence=1.0, privacy_level="restricted",
    ))
    assert verdict.decision is WriteDecision.SKIP
    assert "privacy_restricted" in verdict.reasons


def test_memory_cannot_override_a_safety_warning():
    """**記憶は上書きではなく補正。** 危険の警告を押しのけない。"""
    influence = influence_from(rank(
        [memory("x", memory_id=1, importance=1.0, confidence=1.0,
                event_type="self_failure:kept_talking_after_end_signal")] * 5,
        build_query("危ない", trigger="similar_to_past_failure",
                    failure_patterns=("kept_talking_after_end_signal",)),
    ))
    decision = CognitiveKernel(memory_influence=influence).decide(
        CognitiveEvent(event_type=EventType.GAME_DANGER, confidence=.95),
        build_state(world_state={"danger": .9}),
    )
    assert decision.selected_action is ActionType.WARN


def test_a_self_failure_candidate_uses_a_stable_pattern_name():
    """自由文にすると同じ失敗が別物として溜まる。"""
    candidate = self_failure_candidate("kept_talking_after_end_signal")
    assert candidate.event_type == "self_failure:kept_talking_after_end_signal"
    assert candidate.information_type == InformationType.OBSERVATION


# ---------------------------------------------------------------------------
# Phase 4 の preflight で見つかった3件
#
# どれも「動いているように見えて、いちばん要る場面で効かない」形だった。
# ---------------------------------------------------------------------------


def test_an_open_obligation_does_not_mask_the_end_signal():
    """**義務が1つ残っているだけで「もういいよ」が見えなくなっていた。**

    引き金は1つしか返らないので、順番が優先順位そのものになる。
    話を切りたがっている場面を、義務の再開より先に見る。
    """
    state = build_state(
        user_state={"end_signal": .9, "engagement": .05},
        obligations=("前の説明の続き",),
    )
    assert retrieval_trigger("もういいよ", state) == "similar_to_past_failure"


def test_failure_patterns_come_from_the_state_not_the_trigger_name():
    """引き金は「なぜ探すか」、パターンは「何を探すか」。**別のもの。**"""
    from neuro_voice.mind.episodes import _failure_patterns_for

    state = build_state(
        user_state={"end_signal": .9}, obligations=("続き",),
    )
    patterns = _failure_patterns_for("unresolved_obligation", state)
    assert "kept_talking_after_end_signal" in patterns


def test_repeating_the_same_failure_counts_towards_a_reflection(tmp_path):
    """**同じ失敗の繰り返しが Reflection の閾値へ届かなかった。**

    2回目以降は REINFORCE になり新しい行ができないので、
    「書いた時だけ数える」実装では永久に溜まらない。
    同じ失敗を繰り返すのは、いちばんよくある形なのに。
    """
    memory_service = service(
        tmp_path, **{"memory.episodic_enabled": True, "memory.reflection_enabled": True},
    )
    for _ in range(3):
        verdicts = memory_service.observe_self_failure(
            "kept_talking_after_end_signal", detail="continue_previous_topic:interrupted",
        )
        assert verdicts
    assert memory_service.reflection_due() == "repeated_failures"
    reflection = memory_service.consolidate()
    assert reflection is not None
    assert reflection.support_count >= 3, reflection.snapshot()


# ---------------------------------------------------------------------------
# 機能フラグ・観測可能性
#
# **既定は3つとも false。** 実機で確かめていない層を勝手に本番へ入れない。
# ---------------------------------------------------------------------------


def service(tmp_path, **flags):
    from neuro_voice.mind.episodes import EpisodicMemoryService
    from neuro_voice.mind.store import MemoryStore

    class Cfg:
        def get(self, key, default=None):
            return flags.get(key, default)

    return EpisodicMemoryService(MemoryStore(tmp_path / "mind.db"), Cfg())


def test_the_defaults_are_all_off(tmp_path):
    memory_service = service(tmp_path)
    assert not memory_service.episodic_enabled
    assert not memory_service.retrieval_enabled
    assert not memory_service.reflection_enabled


def test_nothing_is_written_while_the_flag_is_off(tmp_path):
    memory_service = service(tmp_path)
    assert memory_service.observe_turn("単調な一問一答は嫌い", "はい") == []


def test_retrieval_can_be_disabled_independently(tmp_path):
    """**個別に戻せる。** 書くのは続けたいが検索は止めたい、が成り立つ。"""
    memory_service = service(tmp_path, **{"memory.episodic_enabled": True})
    memory_service.observe_turn("単調な一問一答は嫌い", "はい")
    result = memory_service.retrieve("前に言った一問一答の話")
    assert result.trigger == ""
    assert result.scored == ()


def test_a_greeting_never_touches_the_database(tmp_path):
    """検索の判定は文字列の照合だけ。**理由が無い時にDBを開かない。**"""
    memory_service = service(
        tmp_path, **{"memory.episodic_enabled": True, "memory.retrieval_enabled": True},
    )
    result = memory_service.retrieve("おはよう")
    assert result.trigger == ""
    assert result.latency_ms.get("memory_retrieval_ms") is None


def test_the_trace_keeps_no_conversation_text():
    """記憶の記録にも本文を残さない（第12条）。"""
    import json

    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    trace.memory_retrieval_trigger = "past_reference"
    trace.retrieval_query_summary = build_query(
        "口座番号は1234だと前に言ったよね", trigger="past_reference",
    ).summary()
    trace.retrieved_memory_ids = [7]
    text = json.dumps(trace.snapshot(), ensure_ascii=False)
    assert "1234" not in text
    assert "口座番号は" not in text
    assert "past_reference" in text and '"retrieved": [7]' in text


def test_the_trace_records_which_memory_moved_which_action():
    from neuro_voice.cognition.trace import CognitiveTrace

    influence = influence_from(rank(
        [memory("x", memory_id=9, importance=.8, confidence=.9,
                event_type="self_failure:kept_talking_after_end_signal")],
        build_query("もういいよ", trigger="similar_to_past_failure",
                    failure_patterns=("kept_talking_after_end_signal",)),
    ))
    trace = CognitiveTrace()
    trace.memory_effect_on_actions = influence.snapshot()
    snapshot = trace.snapshot()["memory"]["effect_on_actions"]
    assert snapshot["adjustments"]["continue_previous_topic"] < 0
    assert snapshot["evidence"]["continue_previous_topic"] == [9]


def test_a_reflection_needs_the_flag_too(tmp_path):
    memory_service = service(tmp_path, **{"memory.episodic_enabled": True})
    assert memory_service.consolidate() is None
    assert memory_service.reflection_due(session_ended=True) == ""


def test_automatic_prune_retains_all_source_memories(tmp_path):
    """重要度や種類にかかわらず、自動保守は元記録を物理削除しない。"""
    from neuro_voice.mind.store import MemoryStore

    store = MemoryStore(tmp_path / "mind.db")
    keep = store.add_episode("一問一答は嫌い", importance=1, event_type="preference",
                             information_type="user_statement")
    drop = store.add_episode("どうでもいい話", importance=1, event_type="conversation")
    store.prune(max_items=0)
    ids = {row["id"] for row in store.episodes(statuses=("active", "low_priority", "archived"))}
    assert keep in ids, "明示された好みが消えた"
    assert drop in ids, "低重要度だけを理由に元記録を消してはいけない"
    store.close()
