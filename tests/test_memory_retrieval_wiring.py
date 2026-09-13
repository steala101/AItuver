import json
import re
import asyncio
from pathlib import Path
from unittest.mock import patch

import numpy as np

from neuro_voice.cognition.episodic import MemoryCandidate, propose_candidates
from neuro_voice.cognition.trace import CognitiveTrace
from neuro_voice.cognition.recall import retrieval_trigger
from neuro_voice.mind.episodes import EpisodicMemoryService
from neuro_voice.mind.mind import Mind
from neuro_voice.mind.store import MemoryStore


class _Cfg:
    def get(self, _key, default=None):
        return default


def test_strict_store_read_is_bound_to_active_persona(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        store.add_episode("b only", persona_id="b", scope="persona_private")
        store.add_episode("c only", persona_id="c", scope="persona_private")
        assert store.bound_persona_id == "b"
        assert [row["id"] for row in store.all_texts()] == [1]
    finally:
        store.close()


def test_question_never_becomes_a_preference_candidate():
    with patch("neuro_voice.cognition.episodic._PREFERENCE", re.compile("x")):
        assert propose_candidates("x?") == []


def test_new_memory_has_turn_and_persona_provenance(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        service = EpisodicMemoryService(store, _Cfg())
        service.episodic_enabled = True
        service.bind_persona("b")
        candidate = MemoryCandidate(
            "durable assertion", event_type="preference", importance=1,
            novelty=1, expected_future_utility=1, confidence=1,
            provenance={
                "origin_turn_id": "turn-1", "source_role": "user",
                "source_person_id": "local:mic", "persona_id": "b",
                "persona_epoch": 3,
            },
        )
        assert service.commit([candidate])[0].writes
        meta = json.loads(store.episodes(limit=1, persona_id="b")[0]["meta"])
        assert meta["origin_turn_id"] == "turn-1"
        assert meta["persona_id"] == "b"
        assert meta["persona_epoch"] == 3
    finally:
        store.close()


def test_write_gate_leaves_memory_db_and_trace_decisions_unchanged(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        service = EpisodicMemoryService(store, _Cfg())
        service.episodic_enabled = True
        service.write_enabled = False
        service.bind_persona("b")
        candidate = MemoryCandidate(
            "must not persist", event_type="preference", importance=1,
            novelty=1, expected_future_utility=1, confidence=1,
        )
        assert service.commit([candidate]) == []
        assert store.episodes(limit=10, persona_id="b") == []
        assert service.snapshot()["last_write_decisions"] == []
    finally:
        store.close()


def test_semantic_recall_falls_back_to_scoped_text_when_vectors_are_missing(tmp_path):
    class _Embedder:
        def encode(self, _texts, **_kwargs):
            return [np.array([1.0], dtype=np.float32)]

    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        store.add_episode("青い灯台が計測用の合言葉", persona_id="b",
                          scope="persona_private")
        service = EpisodicMemoryService(store, _Cfg())
        service.write_enabled = False
        mind = object.__new__(Mind)
        mind._embedder = _Embedder()
        mind._transcript_enabled = False
        mind._transcript_top_k = 0
        mind._emb_cache = None
        mind._store = store
        mind._episodes = service
        mind._top_k = 3
        mind._last_recall_mode = "semantic"
        recalled, _ = mind._recall_semantic("計測用の合言葉")
        assert [row["id"] for row in recalled] == [1]
        assert mind._last_recall_mode == "lexical_fallback_no_embeddings"
    finally:
        store.close()


def test_explicit_recall_contract_never_invents_and_requires_the_stored_fact():
    mind = object.__new__(Mind)
    mind._last_retrieval_trace = {}
    mind._last_recall_records = []
    missing = mind.recall_response_contract("前に覚えてもらった計測用の合言葉を教えて")
    assert missing["status"] == "NOT_FOUND"
    assert "覚えていない" in missing["reply"]
    assert mind._last_retrieval_trace["recall_status"] == "NOT_FOUND"
    assert mind._last_retrieval_trace["recall_llm_call_count"] == 0

    mind._last_recall_records = [{"id": 1, "text": "合言葉は青い灯台"}]
    found = mind.recall_response_contract("前に覚えてもらった計測用の合言葉を教えて")
    assert found == {"status": "FOUND", "required_fact": "青い灯台", "reply": "合言葉は青い灯台だよ。"}
    assert mind._last_retrieval_trace["recall_evidence_memory_ids"] == [1]

    mind._last_recall_records = []
    asr_variant = mind.recall_response_contract("計測用の合言葉を教えて")
    assert asr_variant["status"] == "NOT_FOUND"
    assert mind._last_retrieval_trace["recall_llm_call_count"] == 0


def test_labelled_recall_contract_handles_plan_number_and_equipment():
    cases = (
        ("計画名", "ノーススター", "計画名を教えて"),
        ("番号", "七二四", "前の番号を覚えてる"),
        ("機器名", "オーロラ", "機器名を教えて"),
    )
    for index, (label, fact, request) in enumerate(cases, start=10):
        mind = object.__new__(Mind)
        mind._last_retrieval_trace = {}
        mind._last_recall_records = [{"id": index, "text": f"{label}は{fact}"}]
        contract = mind.recall_response_contract(request)
        assert contract == {
            "status": "FOUND", "required_fact": fact,
            "reply": f"{label}は{fact}だよ。",
        }
        assert mind._last_retrieval_trace["recall_evidence_memory_ids"] == [index]
        assert mind._last_retrieval_trace["recall_llm_call_count"] == 0


def test_recall_contract_is_prepared_before_trace_capture():
    source = Path("neuro_voice/pipeline.py").read_text(encoding="utf-8")
    assert source.index("recall_contract = self._mind.recall_response_contract(user_text)") < source.index(
        "self._prepare_legacy_turn_trace(metrics)"
    )


def test_trace_has_privacy_safe_retrieval_diagnostics():
    trace = CognitiveTrace(memory_trigger_result="RETRIEVED", retrieval_candidate_count=2)
    diagnostics = trace.snapshot()["memory"]["diagnostics"]
    assert diagnostics["trigger_result"] == "RETRIEVED"
    assert diagnostics["candidate_count"] == 2


def test_normal_conversation_corpus_does_not_trigger_memory_retrieval():
    """The production trigger must not turn ordinary nouns into DB searches."""
    corpus = (
        "空の色を教えて", "猫と犬の違いは？", "今日は少し疲れた",
        "ゲームの面白さって何？", "明日の天気はどうかな", "音楽を聴きたい",
        "散歩に行こうかな", "映画のおすすめを教えて", "コーヒーが飲みたい",
        "今の気分は穏やかだよ",
    ) * 3
    assert len(corpus) == 30
    assert [retrieval_trigger(text) for text in corpus] == [""] * 30


def test_explicit_recall_still_triggers_without_an_llm():
    assert retrieval_trigger("前に話したことを覚えてる？") == "past_reference"
    assert retrieval_trigger("計測用の合言葉を教えて") == "labelled_recall"
    assert retrieval_trigger("この前の約束はどうなった？") == "past_reference"


def test_local_pipeline_passes_the_lightweight_trigger_to_mind():
    """A real Local caller must not reintroduce include_recall=True."""
    from neuro_voice.realtime.context_assembler import ContextAssembler

    class Cfg:
        def get(self, _key, default=None):
            return default

    class MindStub:
        def __init__(self):
            self.calls = []

        def observe_dialogue_turn(self, *_args):
            pass

        async def build_context(self, _text, **kwargs):
            self.calls.append(kwargs)
            return ""

    mind = MindStub()
    assembler = ContextAssembler(Cfg(), mind, lambda *_args, **_kwargs: None)
    asyncio.run(assembler.build("猫と犬の違いは？", []))
    assert mind.calls == [{
        "include_recall": False,
        "retrieval_trigger_reason": "not_applicable",
        "conversation_topic": "",
        "recall_timeout_s": 0.06,
        "source": "local",
        "response_id": "",
        "utterance_id": "",
    }]
    asyncio.run(assembler.build("計測用の合言葉を教えて", []))
    assert mind.calls[-1]["include_recall"] is True
    assert mind.calls[-1]["retrieval_trigger_reason"] == "labelled_recall"
