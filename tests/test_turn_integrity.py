"""Phase 7C: 重複発話・レイテンシ回帰・ペルソナ混入の3つを閉じる。

実機で出たのは3つ。

```
1. たまに同じ内容の文を繰り返す
2. 応答開始が 3000ms から 4000〜5000ms へ悪化
3. ペルソナ変更後、旧ペルソナ固有と思われる反応
```

**推測で3つ同時に直さない。** まず、どこで増えたのか・どこで遅く
なったのか・どこから漏れたのかを言えるようにする。

いちばん守りたいのは4つ:

* **文章比較ではなく ID で一意にする**（意図した繰り返しを消さない）
* **待ち時間と実処理時間を分ける**（「LLMが遅い」で終わらせない）
* **所有者不明を共有にしない**
* **切替前に始まった処理を、切替後へ入れない**
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition.persona_scope import (
    LEGACY_UNSCOPED, SHARED_SCOPES, PersonaContext, PersonaScope, PersonaStamp,
    PersonaSwitch, cache_key, readable, recommended_scope, sql_scope_filter,
)
from neuro_voice.cognition.turn_integrity import (
    CommitKind, ConversationKind, DuplicateSite, TextStage, TurnCommitLedger,
    TurnFrame, collapse_adjacent_duplicates, measure, missing_stages,
    similarity,
)
from neuro_voice.cognition.turn_latency import (
    STAGES, TOTAL, LatencyRecorder, TurnLatency, detect_tool_intent,
    percentile,
)


def frame(**overrides) -> TurnFrame:
    values = {"session_id": "s1", "active_persona_id": "poppo",
              "active_persona_version": "1", "persona_epoch": 0}
    values.update(overrides)
    return TurnFrame(**values)


# ===========================================================================
# 1. 重複の発生地点
# ===========================================================================


def test_a_duplicate_inside_the_generated_text_is_classified_there():
    """**テスト1。** 生成そのものが繰り返している場合。"""
    turn = frame()
    record = turn.note_text(TextStage.LLM_OUTPUT, "確認してみるね。確認してみるね。")
    assert record.duplicate_adjacent_pairs == 1
    assert turn.duplicate_site() is DuplicateSite.LLM_GENERATION_DUPLICATE


def test_the_upstream_layer_wins_the_classification():
    """**上から順に確定させる。**

    生成が既に繰り返しているのに再生回数だけ見ると「再生の問題」に
    見える。直す場所が変わってしまう。
    """
    turn = frame()
    turn.note_text(TextStage.LLM_OUTPUT, "調べてみるね。調べてみるね。")
    site = turn.duplicate_site(speech_requests=2, tts_jobs=2, playbacks=2)
    assert site is DuplicateSite.LLM_GENERATION_DUPLICATE


@pytest.mark.parametrize("counts,expected", [
    ({"speech_requests": 2}, DuplicateSite.SPEECH_REQUEST_DUPLICATE),
    ({"tts_jobs": 2}, DuplicateSite.TTS_JOB_DUPLICATE),
    ({"playbacks": 2}, DuplicateSite.PLAYBACK_DUPLICATE),
    ({}, DuplicateSite.NONE),
])
def test_each_layer_is_distinguishable(counts, expected):
    turn = frame()
    turn.note_text(TextStage.LLM_OUTPUT, "確認してみるね。")
    assert turn.duplicate_site(**counts) is expected


def test_the_stages_are_recorded_without_the_text():
    """**会話全文を通常ログへ残さない**（第17項）。"""
    turn = frame()
    turn.note_text(TextStage.LLM_OUTPUT, "口座番号は1234-5678だよ")
    turn.note_text(TextStage.PLAYBACK_STARTED, "口座番号は1234-5678だよ")
    import json

    text = json.dumps(turn.snapshot(), ensure_ascii=False)
    assert "1234-5678" not in text
    assert "口座番号" not in text
    assert turn.stages[0].char_count > 0
    assert turn.stages[0].text_hash


def test_a_turn_that_stopped_partway_is_visible():
    turn = frame()
    turn.note_text(TextStage.LLM_OUTPUT, "うん")
    turn.note_text(TextStage.SURFACE_REALIZED, "うん")
    missing = missing_stages(turn)
    assert str(TextStage.PLAYBACK_STARTED) in missing
    assert str(TextStage.LLM_OUTPUT) not in missing


def test_the_same_text_at_two_stages_has_the_same_hash():
    """**段階をまたいで同じか**が、ハッシュだけで分かること。"""
    first = measure(TextStage.LLM_OUTPUT, "こんばんは。")
    second = measure(TextStage.TTS_SUBMITTED, "こんばんは")
    assert first.text_hash == second.text_hash


# ===========================================================================
# 2. 一意性の正本
# ===========================================================================


def test_the_same_action_outcome_produces_one_speech_request():
    """**テスト2。** 同じ ActionOutcome を2回配送しても1件。"""
    ledger = TurnCommitLedger()
    first = ledger.claim(CommitKind.SPEECH_REQUEST, "sr-1",
                         decision_id="decision-1")
    second = ledger.claim(CommitKind.SPEECH_REQUEST, "sr-1",
                          decision_id="decision-1")
    assert first.accepted
    assert not second.accepted
    assert second.reason == "already_committed"


def test_one_decision_cannot_produce_two_final_requests():
    """**1 ActionDecision → 0 か 1 の final SpeechRequest。**

    再送で ID が作り直されると、ID を見るだけでは止められない。
    決定側からも数えておく。
    """
    ledger = TurnCommitLedger()
    assert ledger.claim(CommitKind.SPEECH_REQUEST, "sr-1",
                        decision_id="decision-1").accepted
    retried = ledger.claim(CommitKind.SPEECH_REQUEST, "sr-2",
                           decision_id="decision-1")
    assert not retried.accepted
    assert retried.reason == "decision_already_produced_one"


def test_the_same_speech_request_creates_one_tts_job():
    """**テスト3。**"""
    ledger = TurnCommitLedger()
    assert ledger.claim(CommitKind.TTS_JOB, "job-1",
                        decision_id="sr-1").accepted
    assert not ledger.claim(CommitKind.TTS_JOB, "job-1",
                            decision_id="sr-1").accepted


def test_a_redelivered_playback_commit_is_ignored():
    """**テスト4。** 同じ `playback_commit_id` は1回だけ。"""
    ledger = TurnCommitLedger()
    assert ledger.claim(CommitKind.PLAYBACK, "pb-1").accepted
    for _ in range(3):
        assert not ledger.claim(CommitKind.PLAYBACK, "pb-1").accepted


def test_streaming_chunks_may_be_many_but_not_repeated():
    """断片は複数でよい。**同じ断片を二度確定しない。**"""
    ledger = TurnCommitLedger()
    assert ledger.claim_chunk("job-1", 0)
    assert ledger.claim_chunk("job-1", 1)
    assert not ledger.claim_chunk("job-1", 1)
    assert ledger.claim_chunk("job-2", 0)


def test_an_interrupted_commit_can_be_released():
    ledger = TurnCommitLedger()
    ledger.claim(CommitKind.TTS_JOB, "job-1")
    ledger.release(CommitKind.TTS_JOB, "job-1")
    assert ledger.claim(CommitKind.TTS_JOB, "job-1").accepted


def test_an_empty_id_is_not_a_commit():
    """**ID の無いものを通さない。** 通すと数えられなくなる。"""
    ledger = TurnCommitLedger()
    verdict = ledger.claim(CommitKind.PLAYBACK, "")
    assert not verdict.accepted
    assert verdict.reason == "missing_id"


# ===========================================================================
# 3. 隣接重複の軽量防御
# ===========================================================================


def test_an_obvious_adjacent_duplicate_is_collapsed():
    result = collapse_adjacent_duplicates("調べてみるね。調べてみるね。")
    assert result.removed == 1
    assert result.text.count("調べてみるね") == 1


@pytest.mark.parametrize("text", [
    "いや、いや、それは違う",
    "少しずつ。少しずつ進めよう。",
    "本当に？ 本当にそう思う？",
    "まって。まって、いま考えてる。",
])
def test_intentional_repetition_is_kept(text):
    """**テスト5。** 感情表現や意図的な反復を削らない。"""
    result = collapse_adjacent_duplicates(text)
    assert not result.changed, result.text


@pytest.mark.parametrize("text", [
    "いや。いや。", "まって。まって。", "ほんとに。ほんとに。",
    "だめ。だめ。",
])
def test_identical_emphasis_sentences_are_still_kept(text):
    """**強調の繰り返しは、完全に同じでも残す。**

    最初に書いたテストは全部ここへ来ていなかった——「少しずつ。
    少しずつ進めよう。」は文が違うので類似度で弾かれ、
    「いや、いや、それは違う」は1文なので比較すらされない。
    **強調の分岐を消しても全部緑のまま**だった。ここが本当の関門。
    """
    result = collapse_adjacent_duplicates(text)
    assert not result.changed, result.text
    assert result.kept_emphasis >= 1


def test_a_non_emphasis_identical_pair_is_collapsed():
    """対になる確認。**同じ形でも、強調でなければ畳む。**"""
    result = collapse_adjacent_duplicates("確認しておくね。確認しておくね。")
    assert result.changed
    assert result.kept_emphasis == 0


def test_sentences_that_differ_in_facts_are_kept():
    """数字・否定・時制が違えば、**似ていても別のこと**。"""
    for text in ("3個あった。5個あった。",
                 "できるよ。できないよ。",
                 "確認する。確認した。"):
        assert not collapse_adjacent_duplicates(text).changed, text


def test_distant_sentences_are_never_merged():
    """**離れた文を意味の近さで消さない。** 別の話をしている。"""
    text = "調べてみるね。ところで今日は寒いね。調べてみるね。"
    result = collapse_adjacent_duplicates(text)
    assert not result.changed


def test_already_spoken_chunks_are_not_rewritten():
    """**発話済みのストリーミング断片を書き換えない。**"""
    text = "うん。うん。わかった。"
    protected = collapse_adjacent_duplicates(text, already_spoken=2)
    assert not protected.changed
    unprotected = collapse_adjacent_duplicates(text, already_spoken=0)
    assert unprotected.changed


def test_the_defence_uses_no_language_model():
    """**重複除去用の LLM を追加しない**（第17項）。"""
    import inspect

    from neuro_voice.cognition import turn_integrity

    # コメントではなく**呼び出し**が無いことを見る。
    source = inspect.getsource(turn_integrity).lower()
    for banned in ("import openai", "import ollama", ".generate(",
                   "completion(", "self._llm", "await "):
        assert banned not in source, banned


def test_a_long_sentence_pair_is_left_alone():
    """長い文が偶然似ているのは別の話。**畳まない。**"""
    long_text = ("きょうはとてもいい天気だったので少しだけ遠回りをして"
                 "川沿いの道を歩いてから帰ってきたのだけれどとても気持ちよくて"
                 "また明日も同じ道を通ろうと思ったよ")
    assert len(long_text) > 60
    result = collapse_adjacent_duplicates(f"{long_text}。{long_text}。")
    assert not result.changed


def test_similarity_is_only_a_measurement():
    assert similarity("うん", "うん") == 1.0
    assert similarity("はい", "いいえ") < .5


# ===========================================================================
# 4. 通常会話でツール経路へ入らない
# ===========================================================================


@pytest.mark.parametrize("text", [
    "きょうはいい天気だね", "うーん", "そうなんだ", "ポッポはどう思う?",
    "調べてたの?", "調べなくていいよ", "さっき検索しないでって言ったよね",
])
def test_a_normal_utterance_does_not_enter_the_tool_path(text):
    """**テスト6。** Tool Gate も Permission も Confirmation も起動0件。"""
    intent = detect_tool_intent(text)
    assert not intent.wanted, f"{text} → {intent.reason}"
    assert intent.skip_tool_path


@pytest.mark.parametrize("text", [
    "ポッポの語源を調べて", "これ検索して", "メモして保存して",
])
def test_an_explicit_request_does_enter_the_tool_path(text):
    intent = detect_tool_intent(text)
    assert intent.wanted
    assert intent.reason == "explicit_request"


def test_the_intent_check_is_cheap():
    """**判定を賢くするために LLM を呼ばない。** それ自体が遅延になる。"""
    import inspect

    from neuro_voice.cognition import turn_latency

    source = inspect.getsource(turn_latency.detect_tool_intent).lower()
    for banned in ("llm", "generate", "await", "async"):
        assert banned not in source, banned
    intent = detect_tool_intent("きょうはいい天気だね")
    assert intent.decided_ms < 5.0


def test_a_normal_turn_records_that_it_skipped_the_tool_path():
    turn = frame(kind=ConversationKind.NORMAL_CONVERSATION)
    intent = detect_tool_intent("うーん")
    turn.tool_path_entered = intent.wanted
    turn.tool_intent_reason = intent.reason
    assert turn.snapshot()["tool_path_entered"] is False
    assert turn.snapshot()["tool_intent_reason"] == "no_tool_hint"


# ===========================================================================
# 5. レイテンシ
# ===========================================================================


def test_every_required_stage_is_measurable():
    """**テスト7。** 工程ごとに取れること。"""
    turn = TurnLatency(turn_id="t1")
    for stage in STAGES:
        turn.mark(stage, 10.0)
    turn.mark(TOTAL, 160.0)
    snapshot = turn.snapshot()
    for stage in STAGES:
        assert stage in snapshot["stages"], stage
    assert snapshot["total_ms"] == 160.0


def test_queue_time_is_separated_from_work_time():
    """**「LLMが遅い」で終わらせない。** 待ちと実処理を分ける。"""
    turn = TurnLatency(turn_id="t1")
    turn.mark("llm_queue_wait_ms", 800.0)
    turn.mark("llm_first_token_ms", 400.0)
    turn.mark("tts_queue_wait_ms", 200.0)
    turn.mark("tts_first_audio_ms", 300.0)
    assert turn.queue_time == 1000.0
    assert turn.work_time == 700.0


def test_percentiles_not_just_averages():
    """**遅いターンは平均に埋もれる。**"""
    values = [100.0] * 19 + [5000.0]
    assert percentile(values, .50) == 100.0
    assert percentile(values, .95) > 100.0
    average = sum(values) / len(values)
    assert percentile(values, .50) < average


def test_conversation_kinds_are_summarised_separately():
    """ツール会話は元々長い。**混ぜると通常会話の悪化が見えない。**"""
    recorder = LatencyRecorder()
    for _ in range(10):
        normal = TurnLatency(kind=ConversationKind.NORMAL_CONVERSATION)
        normal.mark(TOTAL, 3000.0)
        recorder.record(normal)
        tool = TurnLatency(kind=ConversationKind.TOOL_REQUEST)
        tool.mark(TOTAL, 9000.0)
        recorder.record(tool)

    normal_summary = recorder.summary(kind=ConversationKind.NORMAL_CONVERSATION)
    assert normal_summary[TOTAL].p50 == 3000.0
    mixed = recorder.summary()
    assert mixed[TOTAL].p50 != 3000.0


def test_a_feature_can_be_attributed_a_cost():
    """**どの機能を ON にすると何ms増えるか**を出せること。"""
    recorder = LatencyRecorder()
    for _ in range(10):
        off = TurnLatency()
        off.note_features(memory_retrieval=False)
        off.mark(TOTAL, 3000.0)
        recorder.record(off)
        on = TurnLatency()
        on.note_features(memory_retrieval=True)
        on.mark(TOTAL, 3450.0)
        recorder.record(on)
    report = recorder.compare("memory_retrieval")
    assert report["comparable"]
    assert report["delta_p50_ms"] == 450.0


def test_warmup_turns_can_be_excluded():
    """**モデルの初回読み込みを含めた数字で「遅い」と言わない。**"""
    recorder = LatencyRecorder()
    slow = TurnLatency()
    slow.mark(TOTAL, 20000.0)
    recorder.record(slow)
    for _ in range(9):
        turn = TurnLatency()
        turn.mark(TOTAL, 3000.0)
        recorder.record(turn)
    assert recorder.summary(warmup=1)[TOTAL].worst == 3000.0
    assert recorder.summary(warmup=0)[TOTAL].worst == 20000.0


def test_no_sleep_or_timeout_tuning_hides_the_delay():
    """**sleep や timeout 調整だけで遅延を隠さない**（第17項）。"""
    import inspect

    from neuro_voice.cognition import turn_latency

    source = inspect.getsource(turn_latency)
    assert "time.sleep" not in source


# ===========================================================================
# 6. ペルソナのスコープ
# ===========================================================================


def test_a_persona_can_read_its_own_private_memory():
    """**テスト8。**"""
    verdict = readable(owner_persona_id="A", scope=PersonaScope.PERSONA_PRIVATE,
                       active_persona_id="A")
    assert verdict.allowed


def test_another_persona_cannot_read_it():
    """**テスト9。** A の PERSONA_PRIVATE は B から0件。"""
    verdict = readable(owner_persona_id="A", scope=PersonaScope.PERSONA_PRIVATE,
                       active_persona_id="B")
    assert not verdict.allowed
    assert verdict.reason == "cross_persona"


@pytest.mark.parametrize("persona", ["A", "B"])
def test_explicitly_shared_memory_is_readable_by_both(persona):
    """**テスト10。**"""
    verdict = readable(owner_persona_id="A",
                       scope=PersonaScope.EXPLICIT_SHARED,
                       active_persona_id=persona)
    assert verdict.allowed


def test_an_unowned_legacy_memory_is_not_shared_with_everyone():
    """**テスト11。** 所有者もスコープも無い記憶を全公開しない。"""
    verdict = readable(owner_persona_id="", scope="", active_persona_id="B")
    assert not verdict.allowed
    assert verdict.reason == LEGACY_UNSCOPED
    # 明示的に許可した時だけ出す。
    allowed = readable(owner_persona_id="", scope="", active_persona_id="B",
                       allow_legacy=True)
    assert allowed.allowed


def test_a_scoped_memory_without_an_owner_is_still_isolated():
    """スコープはあるが持ち主が分からない私的情報も、隔離側。"""
    verdict = readable(owner_persona_id="",
                       scope=PersonaScope.PERSONA_PRIVATE,
                       active_persona_id="B")
    assert not verdict.allowed


def test_an_unknown_kind_defaults_to_private():
    """**「所有者不明だから共有」としない。**"""
    assert recommended_scope("何か新しい種類") is PersonaScope.PERSONA_PRIVATE
    assert recommended_scope("reflection") is PersonaScope.PERSONA_PRIVATE
    assert recommended_scope("game_state") is PersonaScope.WORLD_SHARED


def test_only_three_scopes_cross_personas():
    assert SHARED_SCOPES == {
        str(PersonaScope.GLOBAL_SYSTEM), str(PersonaScope.WORLD_SHARED),
        str(PersonaScope.EXPLICIT_SHARED)}
    assert str(PersonaScope.PERSONA_PRIVATE) not in SHARED_SCOPES
    assert str(PersonaScope.SESSION_PERSONA) not in SHARED_SCOPES


# ===========================================================================
# 7. 検索時点での分離
# ===========================================================================


def store_at(tmp_path):
    from neuro_voice.mind.store import MemoryStore

    return MemoryStore(tmp_path / "mind.db")


def add_memory(store, text, *, persona_id="", scope=""):
    memory_id = store.add(text, kind="episode", importance=3)
    with store._lock:
        store._conn.execute(
            "UPDATE memories SET persona_id = ?, scope = ? WHERE id = ?",
            (persona_id, str(scope), int(memory_id)))
        store._conn.commit()
    return memory_id


def test_the_database_query_filters_by_persona(tmp_path):
    """**取ってから捨てるのではなく、取らない。**

    全件取ってから絞ると、`LIMIT` を別人格の記憶が先に埋めて、
    自分の記憶が1件も出てこないのに「漏れてはいない」状態になる。
    """
    store = store_at(tmp_path)
    try:
        add_memory(store, "青い三角形をルミナと呼ぶ", persona_id="A",
                   scope=PersonaScope.PERSONA_PRIVATE)
        add_memory(store, "ポッポの好きな色", persona_id="B",
                   scope=PersonaScope.PERSONA_PRIVATE)
        add_memory(store, "いまKTANEをやっている", persona_id="A",
                   scope=PersonaScope.WORLD_SHARED)

        for_a = store.episodes(limit=50, persona_id="A")
        texts_a = {row["text"] for row in for_a}
        assert "青い三角形をルミナと呼ぶ" in texts_a
        assert "ポッポの好きな色" not in texts_a

        for_b = store.episodes(limit=50, persona_id="B")
        texts_b = {row["text"] for row in for_b}
        assert "青い三角形をルミナと呼ぶ" not in texts_b
        assert "いまKTANEをやっている" in texts_b, "共有スコープが届いていない"
    finally:
        store.close()


def test_legacy_rows_are_excluded_unless_allowed(tmp_path):
    store = store_at(tmp_path)
    try:
        add_memory(store, "所有者不明の記憶")          # persona も scope も空
        assert store.episodes(limit=50, persona_id="A") == []
        allowed = store.episodes(limit=50, persona_id="A",
                                 allow_legacy_unscoped=True)
        assert len(allowed) == 1
    finally:
        store.close()


def test_the_filter_is_applied_in_sql_not_afterwards():
    clause, args = sql_scope_filter("A")
    assert "persona_id = ?" in clause
    assert "scope IN" in clause
    assert args[0] == "A"
    assert set(args[1:]) == SHARED_SCOPES


def test_the_retrieval_service_passes_the_persona_down():
    """**検索側へ人格が届いていること。** 届かないと絞りようがない。"""
    class FakeStore:
        def __init__(self):
            self.calls: list[dict] = []

        def episodes(self, **kwargs):
            self.calls.append(kwargs)
            return []

    from neuro_voice.mind.episodes import EpisodicMemoryService

    class Cfg:
        def get(self, key, default=None):
            return default

    store = FakeStore()
    service = EpisodicMemoryService(store, Cfg())

    service._existing("speaker:1")
    assert "persona_id" not in store.calls[-1], "結ぶ前から渡している"

    service.bind_persona("A", allow_legacy_unscoped=False)
    service._existing("speaker:1")
    assert store.calls[-1]["persona_id"] == "A"
    assert store.calls[-1]["allow_legacy_unscoped"] is False


def test_an_old_store_does_not_silently_return_nothing():
    """**引数を足したせいで検索が静かに0件になる**のを防ぐ。

    `_existing()` は例外を握りつぶすので、古い呼び出し規約の Store に
    新しい引数を渡すと `TypeError` が飲まれて**検索結果が空**になる。
    「漏れてはいないが何も思い出さない」は、いちばん気づきにくい。
    """
    class OldStore:
        def episodes(self, *, limit, speaker_key=""):
            return [{"id": 1, "kind": "episode", "text": "むかしの記憶",
                     "importance": 3, "created_at": 0.0}]

    from neuro_voice.mind.episodes import EpisodicMemoryService

    class Cfg:
        def get(self, key, default=None):
            return default

    service = EpisodicMemoryService(OldStore(), Cfg())
    assert len(service._existing("speaker:1")) == 1


# ===========================================================================
# 8. キャッシュ分離
# ===========================================================================


def test_the_same_input_has_different_cache_keys_per_persona():
    """**テスト15。**"""
    first = cache_key("system_prompt", persona=PersonaContext("A", "1", 0))
    second = cache_key("system_prompt", persona=PersonaContext("B", "1", 0))
    assert first != second


def test_the_cache_key_also_changes_with_version_and_epoch():
    """**`persona_id` だけでは足りない。**

    設定を編集して version が上がっても、切替で epoch が進んでも、
    古い口調のプロンプトが残る。
    """
    base = cache_key("x", persona=PersonaContext("A", "1", 0))
    assert cache_key("x", persona=PersonaContext("A", "2", 0)) != base
    assert cache_key("x", persona=PersonaContext("A", "1", 1)) != base


def test_the_conversation_history_is_dropped_on_switch():
    """**旧ペルソナの AI 発話を、新ペルソナの履歴として渡さない。**

    これが実機で見た漏れの直接の原因。`set_system_prompt()` だけを
    呼んでいたので、system prompt は新しくなるのに履歴が残り、
    モデルから見れば「自分が直前にそう喋った」ことになっていた。
    """
    from neuro_voice.memory.conversation import ConversationManager

    conv = ConversationManager("Aのプロンプト")
    conv.add_user("ルミナって何?")
    conv.add_assistant("青い三角形のことだよ")
    assert len(conv.messages()) == 3

    dropped = conv.switch_persona("Bのプロンプト", persona_id="B")
    assert dropped["history"] == 2
    messages = conv.messages()
    assert len(messages) == 1
    assert messages[0]["content"] == "Bのプロンプト"
    assert conv.persona_id == "B"
    assert "ルミナ" not in str(messages)


def test_the_ui_actually_calls_the_switch():
    """**繋がっていない修正は、修正ではない。**

    `ConversationManager.switch_persona()` を実装しても、UI が
    `set_system_prompt()` を呼び続けていれば履歴は残る。
    Phase 6E で `attach_identity_runtime()` に呼び出し側が無かったのと
    同じ形なので、呼ばれていることを固定しておく。
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "ui"
            / "webview_app.py").read_text(encoding="utf-8")
    start = text.index("composed = build_system_prompt(cfg)")
    body = text[start:start + 1400]
    assert "conv.switch_persona(" in body, "UI から呼ばれていない"
    assert "drop_history_on_persona_switch" in body


def test_the_deferred_topics_do_not_survive_the_switch():
    from neuro_voice.memory.conversation import ConversationManager

    conv = ConversationManager("A")
    conv.add_user("あとで続き聞かせて")
    conv.add_assistant("うん")
    conv.hold_current_topic()
    conv.switch_persona("B", persona_id="B")
    assert conv.deferred_topic is None


# ===========================================================================
# 9. 切替の状態遷移
# ===========================================================================


def test_the_epoch_advances_on_every_switch():
    switch = PersonaSwitch("A")
    assert switch.context.persona_epoch == 0
    switch.switch("B")
    assert switch.context.persona_id == "B"
    assert switch.context.persona_epoch == 1
    switch.switch("A")
    assert switch.context.persona_epoch == 2


def test_a_late_llm_result_from_the_old_persona_is_refused():
    """**テスト13。** A で始めた生成が、B へ切替後に届いた場合。"""
    switch = PersonaSwitch("A")
    stamp = PersonaStamp.of(switch.context)      # A で開始
    assert switch.accepts(stamp)
    switch.switch("B")
    assert not switch.accepts(stamp), "旧ペルソナの結果を受け入れた"


def test_a_late_memory_result_from_the_old_persona_is_refused():
    """**テスト14。** 検索も同じ札で判定する。"""
    switch = PersonaSwitch("A")
    stamp = PersonaStamp.of(switch.context)
    switch.switch("B")
    assert not switch.accepts(stamp)
    fresh = PersonaStamp.of(switch.context)
    assert switch.accepts(fresh)


def test_returning_to_the_same_persona_still_refuses_the_old_result():
    """**A→B→A で、最初の A の結果を受け入れない。**

    `persona_id` だけを見ていると、戻ってきた時に一致してしまう。
    世代まで見る理由がこれ。
    """
    switch = PersonaSwitch("A")
    stamp = PersonaStamp.of(switch.context)
    switch.switch("B")
    switch.switch("A")
    assert switch.context.persona_id == "A"
    assert not switch.accepts(stamp)


def test_nothing_is_accepted_while_switching():
    """切替の途中で書き込むと、**どちらの持ち物か決められない**。"""
    switch = PersonaSwitch("A")
    stamp = PersonaStamp.of(switch.context)
    switch._state = switch.state.__class__.DRAINING
    assert not switch.accepts(stamp)


def test_caches_are_invalidated_on_switch():
    switch = PersonaSwitch("A")
    dropped: list[str] = []
    switch.on_invalidate(lambda old, new: dropped.append("prompt") or "prompt")
    switch.on_invalidate(lambda old, new: dropped.append("tts") or "tts_style")
    record = switch.switch("B")
    assert set(record.invalidated_caches) == {"prompt", "tts_style"}
    assert dropped == ["prompt", "tts"]


def test_inflight_work_is_drained_before_the_epoch_moves():
    order: list[str] = []
    switch = PersonaSwitch("A")
    switch.on_invalidate(lambda old, new: order.append("invalidate") or "c")

    def drain():
        order.append("drain")
        return 3

    record = switch.switch("B", drain=drain)
    assert order == ["drain", "invalidate"], "止める前に世代を進めている"
    assert record.dropped_inflight == 3


def test_consecutive_switches_keep_each_state_separate():
    """**テスト16。** A→B→A で混線しないこと。"""
    switch = PersonaSwitch("A")
    a_first = PersonaStamp.of(switch.context)
    switch.switch("B")
    b_stamp = PersonaStamp.of(switch.context)
    switch.switch("A")
    a_second = PersonaStamp.of(switch.context)

    assert switch.accepts(a_second)
    assert not switch.accepts(b_stamp)
    assert not switch.accepts(a_first)
    assert len(switch.history) == 2


def test_the_switch_record_holds_no_prompt_text():
    """**ペルソナプロンプト全文を通常ログへ残さない**（第17項）。"""
    switch = PersonaSwitch("A")
    record = switch.switch("B")
    import json

    text = json.dumps(record.snapshot(), ensure_ascii=False)
    assert "プロンプト" not in text
    assert set(record.snapshot()) == {
        "switch_id", "from", "to", "from_epoch", "to_epoch", "state",
        "caches", "dropped_inflight"}


# ===========================================================================
# 10. 既存機能の互換
# ===========================================================================


def test_existing_speech_constraints_are_unchanged():
    """**テスト17。** WARN・割込み・沈黙・自発発話・ツール報告。"""
    from neuro_voice.cognition.rollout import (
        SpeechRequest, SpeechSource, check_speech,
    )
    from neuro_voice.cognition.types import (
        ActionType, SpeechPolicy, speech_policy,
    )

    warn = check_speech(
        SpeechRequest(source_type=SpeechSource.GAME_WARNING_FAST_PATH,
                      source_action=ActionType.WARN, confidence=.9),
        cognition_enabled=True)
    assert warn.allowed

    silent = check_speech(
        SpeechRequest(source_type=SpeechSource.COGNITIVE_DECISION,
                      source_action=ActionType.REMAIN_SILENT),
        cognition_enabled=True, has_valid_decision=True,
        decision_allows_speech=False)
    assert not silent.allowed

    proactive = check_speech(
        SpeechRequest(source_type=SpeechSource.PROACTIVE_OPPORTUNITY,
                      source_action=ActionType.COMMENT, confidence=.9),
        cognition_enabled=True, has_valid_decision=True,
        decision_allows_speech=True)
    assert proactive.allowed

    unverified = check_speech(
        SpeechRequest(source_type=SpeechSource.COGNITIVE_TOOL_OUTCOME,
                      source_action=ActionType.REPORT_TOOL_SUCCESS,
                      execution_id="e", tool_outcome_event_id="ev",
                      outcome_verified=False),
        cognition_enabled=True, has_valid_decision=True,
        decision_allows_speech=True)
    assert not unverified.allowed

    assert speech_policy(ActionType.REMAIN_SILENT) is SpeechPolicy.FORBIDDEN


def test_the_old_retrieval_path_still_works_without_a_persona(tmp_path):
    """`persona_id` を渡さなければ、従来どおり全件が候補になる。"""
    store = store_at(tmp_path)
    try:
        add_memory(store, "むかしの記憶")
        assert len(store.episodes(limit=50)) == 1
    finally:
        store.close()


def test_all_phase_seven_c_flags_are_conservative():
    from pathlib import Path

    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))
    turn = data["turn_integrity"]
    # **2026-08-04 に有効化した。** 所有者不明の記憶（neuro 202件 /
    # luna 12件）を移行し終えたので、隠れるものが無くなった。移行前に
    # 上げると既存の記憶が全部見えなくなる——順序に意味がある。
    assert turn["persona_scope_enabled"] is True
    # **こちらは上げない。** 「たぶん誰のでもない」で全ペルソナへ
    # 公開すると、実際には誰かの私的な記憶だったものが漏れる。
    assert turn["allow_legacy_unscoped_memory"] is False
    assert turn["collapse_adjacent_duplicates"] is False
    assert turn["latency_profiling_enabled"] is False
    # **通常会話のツール経路スキップだけは既定で有効。**
    # 何もしないのが正しい既定なので、上げないと払い続ける。
    assert turn["skip_tool_path_without_intent"] is True
